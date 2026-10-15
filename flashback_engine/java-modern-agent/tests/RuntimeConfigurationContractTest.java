package local.flashback.modern.impl;

import com.google.gson.*;
import java.io.*;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

/** Uses the production writer with an actual capture's registry snapshot, without a game JVM. */
public final class RuntimeConfigurationContractTest {
    static void vi(DataOutputStream out,int value)throws IOException {do{int next=value&127;value>>>=7;out.writeByte(next|(value==0?0:128));}while(value!=0);}
    static byte[] packet(int id,int entity)throws IOException {ByteArrayOutputStream bytes=new ByteArrayOutputStream();DataOutputStream out=new DataOutputStream(bytes);vi(out,id);vi(out,entity);out.writeByte(0);return bytes.toByteArray();}
    public static void main(String[] args)throws Exception {
        Path original=Path.of(args[0]),output=Path.of(args[1]);Files.createDirectories(output);
        for(String file:List.of("snapshot-config.bin","snapshot-packets.bin"))Files.copy(original.resolve(file),output.resolve(file),StandardCopyOption.REPLACE_EXISTING);
        Files.write(output.resolve("snapshot-chunks.bin"),new byte[0]);
        Path fbrl;try(var files=Files.list(original)){fbrl=files.filter(path->path.toString().endsWith(".fbrl")).findFirst().orElseThrow();}
        JsonObject meta;try(BufferedReader reader=Files.newBufferedReader(fbrl)){meta=JsonParser.parseString(reader.readLine()).getAsJsonObject();}
        int initial=meta.get("playerEntityId").getAsInt();
        Files.writeString(output.resolve("replay-player.properties"),"sourcePlayerId="+initial+"\nplayerUuid="+meta.get("playerUuid").getAsString()+"\nplayerNameBase64="+Base64.getEncoder().encodeToString(meta.get("playerName").getAsString().getBytes(StandardCharsets.UTF_8))+"\ngametype="+meta.get("gametype").getAsInt()+"\nsourceProtocol=773\n");
        Files.writeString(output.resolve("capture.properties"),"captureFormat=5\nagentVersion=17\nsourceProtocol=773\nlocalHotbarSlots=true\ncaptureClosed=true\n");
        List<byte[]> configuration=new ArrayList<>();
        try(DataInputStream input=new DataInputStream(Files.newInputStream(output.resolve("snapshot-config.bin")))){while(input.available()>0){int size=input.readInt();byte[] bytes=input.readNBytes(size);if(bytes.length!=size)throw new EOFException();configuration.add(bytes);}}
        byte[] login=Files.readAllBytes(output.resolve("snapshot-packets.bin"));int idBytes=1;while((login[idBytes-1]&128)!=0)idBytes++;
        byte[] login77=login.clone(),login99=login.clone();ByteBuffer.wrap(login77,idBytes,4).putInt(77);ByteBuffer.wrap(login99,idBytes,4).putInt(99);
        // Packet IDs supplied from the installed converter's source protocol enum.
        Class<?> packetTypes=Class.forName("com.replaymod.replaystudio.lib.viaversion.protocols.v1_21_7to1_21_9.packet.ClientboundPackets1_21_9");
        int start=(int)packetTypes.getMethod("getId").invoke(packetTypes.getField("START_CONFIGURATION").get(null));
        int animate=(int)packetTypes.getMethod("getId").invoke(packetTypes.getField("ANIMATE").get(null));
        try(DataOutputStream runtime=new DataOutputStream(Files.newOutputStream(output.resolve("runtime-packets.bin")));
                DataOutputStream config=new DataOutputStream(Files.newOutputStream(output.resolve("runtime-configurations.bin")))) {
            Recorder.writeRuntimeFrame(runtime,config,1,0,packet(animate,initial),List.of());
            Recorder.writeRuntimeFrame(runtime,config,2,1,new byte[]{(byte)start},List.of());
            Recorder.writeRuntimeFrame(runtime,config,3,2,login77,configuration);
            Recorder.writeRuntimeFrame(runtime,config,4,3,packet(animate,77),List.of());
            Recorder.writeRuntimeFrame(runtime,config,7,4,login99,configuration); // Reconnect can replace connection without START.
            Recorder.writeRuntimeFrame(runtime,config,8,5,packet(animate,99),List.of());
            Recorder.writeRuntimeFrame(runtime,config,9,6,new byte[]{(byte)start},List.of()); // User stops while waiting.
        }
        try(DataInputStream input=new DataInputStream(Files.newInputStream(output.resolve("runtime-configurations.bin")))) {
            for(long[] expected:List.of(new long[]{3,2},new long[]{7,4})) {
                if(input.readLong()!=expected[0]||input.readLong()!=expected[1]||input.readInt()!=configuration.size())throw new AssertionError("Configuration ordinal/tick mismatch");
                for(byte[] expectedPacket:configuration){int size=input.readInt();if(!Arrays.equals(input.readNBytes(size),expectedPacket))throw new AssertionError("Configuration packet changed");}
            }
            if(input.available()!=0)throw new AssertionError("Unexpected configuration trailing bytes");
        }
        System.out.println("PASS production runtime writer: exact LOGIN ordinals 2/4, ticks 3/7, complete registry bytes, reconnect without START, final pending transfer. "+output);
    }
}
