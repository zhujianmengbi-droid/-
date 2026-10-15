package local.flashback.modern.impl;
import java.util.jar.*;
import org.objectweb.asm.*;
import org.objectweb.asm.tree.*;
import org.objectweb.asm.tree.analysis.*;
public final class ReceiverHookContractTest {
    public static void main(String[] args)throws Exception {
        ReceiverHook hook=new ReceiverHook();byte[] changed;
        try(JarFile jar=new JarFile(args[0])){byte[] original=jar.getInputStream(jar.getJarEntry(ReceiverHook.TARGET+".class")).readAllBytes();changed=hook.transform(null,null,ReceiverHook.TARGET,null,null,original);}
        if(changed==null||!hook.login||hook.methods!=122)throw new AssertionError("Expected all 122 client packet handlers and explicit LOGIN receiver");
        ClassNode node=new ClassNode();new ClassReader(changed).accept(node,0);int methods=0,callbacks=0;
        for(MethodNode method:node.methods)if((method.access&(Opcodes.ACC_ABSTRACT|Opcodes.ACC_NATIVE))==0) {
            new Analyzer<BasicValue>(new BasicVerifier()).analyze(node.name,method);methods++;
            for(AbstractInsnNode instruction:method.instructions)if(instruction instanceof MethodInsnNode call&&call.owner.equals("local/flashback/modern/bridge/PacketBridgeV2")&&call.name.equals("capture")) {
                callbacks++;
                AbstractInsnNode[] code=method.instructions.toArray();
                int receiverStores=0,packetStores=0;
                for(int i=0;i<Math.min(16,code.length);i++)if(code[i] instanceof VarInsnNode variable&&variable.getOpcode()==Opcodes.ALOAD){if(variable.var==0)receiverStores++;if(variable.var==1)packetStores++;}
                if(receiverStores==0||packetStores==0)throw new AssertionError("Callback must carry this and packet: "+method.name);
            }
        }
        if(callbacks!=122)throw new AssertionError("Duplicate or missing receiver callbacks");
        System.out.println("PASS receiver hook: 122 explicit this+packet callbacks, LOGIN included, "+methods+" method operand stacks verified");
    }
}
