package local.flashback.modern.impl;

import java.lang.instrument.ClassFileTransformer;
import java.security.ProtectionDomain;
import org.objectweb.asm.*;

/** Receives the new listener itself: Minecraft.getConnection still refers to the old player at LOGIN entry. */
final class ReceiverHook implements ClassFileTransformer {
    static final String TARGET="net/minecraft/client/multiplayer/ClientPacketListener";
    volatile int methods;
    volatile boolean login;
    static boolean target(String name){return TARGET.equals(name.replace('.','/'));}
    @Override public byte[] transform(Module module,ClassLoader loader,String name,Class<?> type,ProtectionDomain domain,byte[] bytes){
        if(name==null||!target(name))return null;
        ClassReader reader=new ClassReader(bytes);ClassWriter writer=new ClassWriter(reader,ClassWriter.COMPUTE_MAXS);
        int[] count={0};boolean[] foundLogin={false};
        reader.accept(new ClassVisitor(Opcodes.ASM9,writer){
            @Override public MethodVisitor visitMethod(int access,String method,String descriptor,String signature,String[] exceptions){
                MethodVisitor next=super.visitMethod(access,method,descriptor,signature,exceptions);
                if((access&(Opcodes.ACC_STATIC|Opcodes.ACC_ABSTRACT|Opcodes.ACC_NATIVE|Opcodes.ACC_SYNTHETIC))!=0||(access&Opcodes.ACC_PUBLIC)==0)return next;
                Type[] args=Type.getArgumentTypes(descriptor);
                if(args.length!=1||args[0].getSort()!=Type.OBJECT||Type.getReturnType(descriptor).getSort()!=Type.VOID
                        ||!args[0].getInternalName().startsWith("net/minecraft/network/protocol/game/Clientbound"))return next;
                count[0]++;if(args[0].getInternalName().endsWith("/ClientboundLoginPacket"))foundLogin[0]=true;
                return new MethodVisitor(Opcodes.ASM9,next){@Override public void visitCode(){
                    super.visitCode();super.visitInsn(Opcodes.ICONST_2);super.visitTypeInsn(Opcodes.ANEWARRAY,"java/lang/Object");
                    super.visitInsn(Opcodes.DUP);super.visitInsn(Opcodes.ICONST_0);super.visitVarInsn(Opcodes.ALOAD,0);super.visitInsn(Opcodes.AASTORE);
                    super.visitInsn(Opcodes.DUP);super.visitInsn(Opcodes.ICONST_1);super.visitVarInsn(Opcodes.ALOAD,1);super.visitInsn(Opcodes.AASTORE);
                    super.visitMethodInsn(Opcodes.INVOKESTATIC,"local/flashback/modern/bridge/PacketBridgeV2","capture","(Ljava/lang/Object;)V",false);
                }};
            }
        },0);
        methods=count[0];login=foundLogin[0];return methods==0?null:writer.toByteArray();
    }
}
