package local.flashback.modern;

import java.io.*;
import java.lang.instrument.*;
import java.net.*;
import java.nio.file.*;
import java.security.ProtectionDomain;
import java.util.*;
import java.util.jar.*;
import java.util.function.Consumer;
import org.objectweb.asm.*;

/** Java 21 agent entry point; implementation is isolated in the game class loader. */
public final class AgentV2 {
    private static JarFile bridgeJar;
    private static URLClassLoader implementationLoader;
    private static boolean installed;
    private static Object recorder;
    private static Hook installedHook;
    private static EffectHook installedEffects;
    private static Instrumentation installedInstrumentation;
    public static synchronized void agentmain(String options, Instrumentation inst) throws Exception {
        retireLegacy();
        if (installed) {
            if (Boolean.TRUE.equals(recorder.getClass().getMethod("isRecording").invoke(recorder)))
                throw new IllegalStateException("Stop the active recording before changing the recorder session");
            recorder.getClass().getMethod("shutdown").invoke(recorder);
            Class.forName("local.flashback.modern.bridge.PacketBridgeV2",true,null).getField("sink").set(null,null);
            installedInstrumentation.removeTransformer(installedHook);
            if(installedEffects!=null)installedInstrumentation.removeTransformer(installedEffects);
            for(Class<?> type:installedInstrumentation.getAllLoadedClasses())if((Hook.target(type.getName()) || EffectHook.target(type.getName())) && installedInstrumentation.isModifiableClass(type))installedInstrumentation.retransformClasses(type);
            implementationLoader.close();installed=false;
        }
        Path state = null;
        for (String item : options.split(",")) if (item.startsWith("state=")) state = Path.of(item.substring(6)).toAbsolutePath();
        if (state == null) throw new IllegalArgumentException("state= directory is required");
        Files.createDirectories(state);
        Class<?> minecraft = Arrays.stream(inst.getAllLoadedClasses())
                .filter(c -> c.getName().equals("net.minecraft.client.Minecraft")).findFirst()
                .orElseThrow(() -> new IllegalStateException("Minecraft 1.21.10 client is not loaded"));
        Class<?> constants=Class.forName("net.minecraft.SharedConstants",false,minecraft.getClassLoader());
        Object version=constants.getMethod("getCurrentVersion").invoke(null);
        Class<?> versionInterface=Class.forName("net.minecraft.WorldVersion",false,minecraft.getClassLoader());
        if(!"1.21.10".equals(versionInterface.getMethod("name").invoke(version)) || ((Number)versionInterface.getMethod("protocolVersion").invoke(version)).intValue()!=773)
            throw new IllegalStateException("Modern recorder requires Minecraft 1.21.10 / protocol 773");
        URL self = AgentV2.class.getProtectionDomain().getCodeSource().getLocation();
        for(String item:options.split(","))if(item.startsWith("implementation="))self=Path.of(item.substring(15)).toUri().toURL();
        Path bridgePath = state.resolve("modern-packet-bootstrap-bridge.jar");
        if(bridgeJar==null) {
        try (JarOutputStream output = new JarOutputStream(Files.newOutputStream(bridgePath));
             InputStream input = AgentV2.class.getResourceAsStream("/local/flashback/modern/bridge/PacketBridgeV2.class")) {
            if (input == null) throw new FileNotFoundException("PacketBridgeV2.class");
            output.putNextEntry(new JarEntry("local/flashback/modern/bridge/PacketBridgeV2.class"));
            input.transferTo(output); output.closeEntry();
        }
        bridgeJar = new JarFile(bridgePath.toFile());
        inst.appendToBootstrapClassLoaderSearch(bridgeJar);
        }
        Class<?> bridge = Class.forName("local.flashback.modern.bridge.PacketBridgeV2", true, null);
        if (minecraft.getModule().isNamed()) inst.redefineModule(minecraft.getModule(), Set.of(bridge.getModule()), Map.of(), Map.of(), Set.of(), Map.of());
        implementationLoader = new URLClassLoader(new URL[]{self}, minecraft.getClassLoader()) {
            @Override protected Class<?> loadClass(String name, boolean resolve) throws ClassNotFoundException {
                if (name.startsWith("local.flashback.modern.impl.")) {
                    synchronized (getClassLoadingLock(name)) {
                        Class<?> value = findLoadedClass(name);
                        if (value == null) value = findClass(name);
                        if (resolve) resolveClass(value);
                        return value;
                    }
                }
                return super.loadClass(name, resolve);
            }
        };
        if(minecraft.getModule().isNamed()) {
            Map<String,Set<Module>> opens=new HashMap<>();
            for(String name:List.of("net.minecraft.client.multiplayer","net.minecraft.network.protocol.game","net.minecraft.network.syncher","net.minecraft.tags"))
                opens.put(name,Set.of(implementationLoader.getUnnamedModule()));
            inst.redefineModule(minecraft.getModule(),Set.of(),Map.of(),opens,Set.of(),Map.of());
        }
        for(Class<?> skin:inst.getAllLoadedClasses()) if(skin.getName().equals("com.netease.mc.mod.skin.SkinHandler") && skin.getModule().isNamed())
            inst.redefineModule(skin.getModule(),Set.of(),Map.of(),Map.of("com.netease.mc.mod.skin",Set.of(implementationLoader.getUnnamedModule())),Set.of(),Map.of());
        Class<?> implementation = Class.forName("local.flashback.modern.impl.Recorder", true, implementationLoader);
        recorder = implementation.getConstructor(Path.class).newInstance(state);
        bridge.getField("sink").set(null, (Consumer<Object>) recorder);
        Hook hook = new Hook();
        inst.addTransformer(hook, true);
        int targets = 0;
        EffectHook effects=new EffectHook();
        inst.addTransformer(effects,true);
        try {
            for (Class<?> value : inst.getAllLoadedClasses()) if (Hook.target(value.getName())) {
                if (!inst.isModifiableClass(value)) throw new IllegalStateException("Packet listener is not modifiable");
                inst.retransformClasses(value); targets++;
            }
            if (targets == 0 || hook.methods == 0) throw new IllegalStateException("No verified packet handlers found");
            for(Class<?> type:inst.getAllLoadedClasses())if(EffectHook.target(type.getName()) && inst.isModifiableClass(type))inst.retransformClasses(type);
            boolean effectsSupported=effects.levelSites==4 && effects.modeSites==1;
            implementation.getMethod("effectsAvailable",boolean.class).invoke(recorder,effectsSupported);
            installedHook=hook; installedEffects=effects; installedInstrumentation=inst;
            URLClassLoader currentLoader=implementationLoader;
            implementation.getMethod("onExit",Runnable.class).invoke(recorder,(Runnable)()->release(inst,hook,effects,currentLoader,bridge));
            installed = true;
            implementation.getMethod("start", int.class).invoke(recorder, hook.methods);
        } catch (Throwable error) {
            bridge.getField("sink").set(null, null);
            inst.removeTransformer(hook); inst.removeTransformer(effects);
            for (Class<?> value : inst.getAllLoadedClasses()) if ((Hook.target(value.getName()) || EffectHook.target(value.getName())) && inst.isModifiableClass(value)) try { inst.retransformClasses(value); } catch (Throwable ignored) {}
            try{implementation.getMethod("shutdown").invoke(recorder);}catch(Throwable ignored){}
            try{implementationLoader.close();}catch(Throwable ignored){}
            installed=false;
            throw error;
        }
    }
    private static void retireLegacy() throws Exception {
        Class<?> legacy;
        try{legacy=Class.forName("local.flashback.modern.Agent",false,ClassLoader.getSystemClassLoader());}catch(ClassNotFoundException absent){return;}
        java.lang.reflect.Field flag=legacy.getDeclaredField("installed");flag.setAccessible(true);
        if(!flag.getBoolean(null))return;
        java.lang.reflect.Field recorderField=legacy.getDeclaredField("recorder");recorderField.setAccessible(true);Object previous=recorderField.get(null);
        if(Boolean.TRUE.equals(previous.getClass().getMethod("isRecording").invoke(previous)))throw new IllegalStateException("Stop the previous recorder before upgrading");
        previous.getClass().getMethod("shutdown").invoke(previous);
        java.lang.reflect.Field instrumentField=legacy.getDeclaredField("installedInstrumentation"),hookField=legacy.getDeclaredField("installedHook");
        instrumentField.setAccessible(true);hookField.setAccessible(true);Instrumentation old=(Instrumentation)instrumentField.get(null);
        old.removeTransformer((ClassFileTransformer)hookField.get(null));
        for(Class<?> type:old.getAllLoadedClasses())if(Hook.target(type.getName()) && old.isModifiableClass(type))old.retransformClasses(type);
        Class.forName("local.flashback.modern.bridge.PacketBridge",true,null).getField("sink").set(null,null);
        flag.setBoolean(null,false);
        java.lang.reflect.Field loaderField=legacy.getDeclaredField("implementationLoader");loaderField.setAccessible(true);
        URLClassLoader oldLoader=(URLClassLoader)loaderField.get(null);if(oldLoader!=null)oldLoader.close();
        loaderField.set(null,null);recorderField.set(null,null);hookField.set(null,null);
    }
    private static void release(Instrumentation inst,Hook hook,EffectHook effects,URLClassLoader loader,Class<?> bridge){
        // No Agent monitor is acquired here: agentmain may be waiting for this worker to exit.
        try{bridge.getField("sink").set(null,null);}catch(Throwable ignored){}
        inst.removeTransformer(hook);inst.removeTransformer(effects);
        for(Class<?> type:inst.getAllLoadedClasses())if((Hook.target(type.getName())||EffectHook.target(type.getName()))&&inst.isModifiableClass(type))try{inst.retransformClasses(type);}catch(Throwable ignored){}
        try{loader.close();}catch(IOException ignored){}
        if(installedHook==hook)installed=false;
    }
    private static final class Hook implements ClassFileTransformer {
        volatile int methods;
        static boolean target(String name) { return name.replace('.', '/').equals("net/minecraft/client/multiplayer/ClientPacketListener"); }
        @Override public byte[] transform(Module module, ClassLoader loader, String name, Class<?> type, ProtectionDomain domain, byte[] bytes) {
            if (name == null || !target(name)) return null;
            ClassReader reader = new ClassReader(bytes);
            ClassWriter writer = new ClassWriter(reader, ClassWriter.COMPUTE_MAXS);
            int[] count = {0};
            reader.accept(new ClassVisitor(Opcodes.ASM9, writer) {
                @Override public MethodVisitor visitMethod(int access, String method, String descriptor, String signature, String[] exceptions) {
                    MethodVisitor next = super.visitMethod(access, method, descriptor, signature, exceptions);
                    if ((access & (Opcodes.ACC_STATIC | Opcodes.ACC_ABSTRACT | Opcodes.ACC_NATIVE | Opcodes.ACC_SYNTHETIC)) != 0 || (access & Opcodes.ACC_PUBLIC) == 0) return next;
                    Type[] args = Type.getArgumentTypes(descriptor);
                    if (args.length != 1 || args[0].getSort() != Type.OBJECT || Type.getReturnType(descriptor).getSort() != Type.VOID) return next;
                    if (!args[0].getInternalName().startsWith("net/minecraft/network/protocol/game/Clientbound")) return next;
                    count[0]++;
                    return new MethodVisitor(Opcodes.ASM9, next) {
                        @Override public void visitCode() {
                            super.visitCode(); super.visitVarInsn(Opcodes.ALOAD, 1);
                            super.visitMethodInsn(Opcodes.INVOKESTATIC, "local/flashback/modern/bridge/PacketBridgeV2", "capture", "(Ljava/lang/Object;)V", false);
                        }
                    };
                }
            }, 0);
            methods = count[0]; return count[0] == 0 ? null : writer.toByteArray();
        }
    }
}
