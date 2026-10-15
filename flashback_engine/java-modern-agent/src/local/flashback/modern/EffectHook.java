package local.flashback.modern;
import java.lang.instrument.ClassFileTransformer;
import java.security.ProtectionDomain;
import org.objectweb.asm.*;

/** Narrow hooks for actual local world effects, with no change to original control flow. */
final class EffectHook implements ClassFileTransformer {
    static final String LEVEL="net/minecraft/client/multiplayer/ClientLevel", MODE="net/minecraft/client/multiplayer/MultiPlayerGameMode";
    volatile int levelSites,modeSites;
    static boolean target(String name){name=name.replace('.','/');return name.equals(LEVEL)||name.equals(MODE);}
    @Override public byte[] transform(Module module,ClassLoader loader,String name,Class<?> type,ProtectionDomain domain,byte[] bytes){
        if(name==null||!target(name))return null;
        ClassReader reader=new ClassReader(bytes);ClassWriter writer=new ClassWriter(reader,ClassWriter.COMPUTE_MAXS);int[] count={0};
        reader.accept(new ClassVisitor(Opcodes.ASM9,writer){
            @Override public MethodVisitor visitMethod(int access,String method,String desc,String signature,String[] exceptions){
                MethodVisitor next=super.visitMethod(access,method,desc,signature,exceptions);
                if((access&(Opcodes.ACC_STATIC|Opcodes.ACC_NATIVE|Opcodes.ACC_ABSTRACT))!=0)return next;
                int kind=-1;
                if(name.equals(LEVEL)){
                    if(method.equals("playSound")&&desc.equals("(DDDLnet/minecraft/sounds/SoundEvent;Lnet/minecraft/sounds/SoundSource;FFZJ)V"))kind=0;
                    else if(method.equals("levelEvent")&&desc.equals("(Lnet/minecraft/world/entity/Entity;ILnet/minecraft/core/BlockPos;I)V"))kind=1;
                    else if(method.equals("globalLevelEvent")&&desc.equals("(ILnet/minecraft/core/BlockPos;I)V"))kind=2;
                    else if(method.equals("setBlock")&&desc.equals("(Lnet/minecraft/core/BlockPos;Lnet/minecraft/world/level/block/state/BlockState;II)Z"))kind=3;
                }
                if(kind>=0){final int selected=kind;count[0]++;return new MethodVisitor(Opcodes.ASM9,next){
                    @Override public void visitCode(){super.visitCode();if(selected!=0 && selected!=3)emit(this,selected,desc,false);}
                    @Override public void visitInsn(int opcode){
                        if(selected==0&&opcode==Opcodes.RETURN)emit(this,selected,desc,false);
                        if(selected==3&&opcode==Opcodes.IRETURN){super.visitInsn(Opcodes.DUP);emit(this,selected,desc,true);}
                        super.visitInsn(opcode);
                    }
                };}
                if(name.equals(MODE)&&method.equals("continueDestroyBlock")&&desc.equals("(Lnet/minecraft/core/BlockPos;Lnet/minecraft/core/Direction;)Z"))return new MethodVisitor(Opcodes.ASM9,next){
                    @Override public void visitMethodInsn(int opcode,String owner,String method,String descriptor,boolean itf){
                        if(opcode==Opcodes.INVOKEVIRTUAL&&owner.equals("net/minecraft/client/sounds/SoundManager")&&method.equals("play")&&descriptor.equals("(Lnet/minecraft/client/resources/sounds/SoundInstance;)Lnet/minecraft/client/sounds/SoundEngine$PlayResult;")){
                            super.visitInsn(Opcodes.DUP_X1);super.visitMethodInsn(opcode,owner,method,descriptor,itf);
                            super.visitInsn(Opcodes.SWAP);
                            super.visitMethodInsn(Opcodes.INVOKESTATIC,"local/flashback/modern/bridge/PacketBridgeV2","hitSound","(Ljava/lang/Object;)V",false);count[0]++;return;
                        }
                        super.visitMethodInsn(opcode,owner,method,descriptor,itf);
                    }
                };
                return next;
            }
        },0);
        int expected=name.equals(LEVEL)?4:1;
        if(count[0]!=expected)return null;
        if(name.equals(LEVEL))levelSites=count[0];else modeSites=count[0];
        return writer.toByteArray();
    }
    private static void emit(MethodVisitor mv,int kind,String desc,boolean checkSuccess){
        Type[] args=Type.getArgumentTypes(desc);mv.visitIntInsn(Opcodes.BIPUSH,args.length+2);mv.visitTypeInsn(Opcodes.ANEWARRAY,"java/lang/Object");
        mv.visitInsn(Opcodes.DUP);push(mv,0);push(mv,kind);box(mv,Type.INT_TYPE);mv.visitInsn(Opcodes.AASTORE);
        mv.visitInsn(Opcodes.DUP);push(mv,1);mv.visitVarInsn(Opcodes.ALOAD,0);mv.visitInsn(Opcodes.AASTORE);
        int slot=1;for(int i=0;i<args.length;i++){mv.visitInsn(Opcodes.DUP);push(mv,i+2);mv.visitVarInsn(args[i].getOpcode(Opcodes.ILOAD),slot);box(mv,args[i]);mv.visitInsn(Opcodes.AASTORE);slot+=args[i].getSize();}
        if(checkSuccess){mv.visitInsn(Opcodes.SWAP);mv.visitMethodInsn(Opcodes.INVOKESTATIC,"local/flashback/modern/bridge/PacketBridgeV2","captureSuccessful","(Ljava/lang/Object;Z)V",false);}
        else mv.visitMethodInsn(Opcodes.INVOKESTATIC,"local/flashback/modern/bridge/PacketBridgeV2","capture","(Ljava/lang/Object;)V",false);
    }
    private static void push(MethodVisitor mv,int i){mv.visitIntInsn(Opcodes.BIPUSH,i);}
    private static void box(MethodVisitor mv,Type t){String type=switch(t.getSort()){case Type.INT->"Integer";case Type.BOOLEAN->"Boolean";case Type.FLOAT->"Float";case Type.DOUBLE->"Double";case Type.LONG->"Long";default->null;};if(type!=null)mv.visitMethodInsn(Opcodes.INVOKESTATIC,"java/lang/"+type,"valueOf","("+t.getDescriptor()+")Ljava/lang/"+type+";",false);}
}
