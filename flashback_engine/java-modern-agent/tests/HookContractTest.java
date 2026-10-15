package local.flashback.modern;
import java.nio.file.*;
import java.util.jar.*;
import org.objectweb.asm.*;
import org.objectweb.asm.tree.*;
import org.objectweb.asm.tree.analysis.*;

/** Verify injected operand stacks against the exact installed Minecraft class bytes, without starting Minecraft. */
public final class HookContractTest {
    public static void main(String[] args)throws Exception{
        EffectHook hook=new EffectHook();
        try(JarFile jar=new JarFile(args[0])){
            for(String target:new String[]{EffectHook.LEVEL,EffectHook.MODE}){
                byte[] original=jar.getInputStream(jar.getJarEntry(target+".class")).readAllBytes();
                byte[] changed=hook.transform(null,null,target,null,null,original);
                if(changed==null)throw new AssertionError("No exact effect sites: "+target);
                ClassNode node=new ClassNode();new ClassReader(changed).accept(node,0);
                int methods=0;
                for(MethodNode method:node.methods)if((method.access&(Opcodes.ACC_ABSTRACT|Opcodes.ACC_NATIVE))==0){
                    new Analyzer<BasicValue>(new BasicVerifier()).analyze(node.name,method);methods++;
                }
                System.out.println("PASS "+target+" operand stacks verified in "+methods+" methods");
            }
        }
        if(hook.levelSites!=4 || hook.modeSites!=1)throw new AssertionError("Expected exactly 4 level and 1 hit-sound sites");
        System.out.println("PASS exact hook sites: 4 + 1");
    }
}
