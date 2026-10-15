package local.flashback.modern.impl;

import com.google.common.collect.ImmutableMultimap;
import com.mojang.authlib.GameProfile;
import com.mojang.authlib.properties.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import net.minecraft.network.protocol.game.ClientboundPlayerInfoUpdatePacket;
import net.minecraft.world.level.GameType;

/** Offline regression: omitted NPC identities and placeholder texture replacement. */
public final class ProfileSkinContractTest {
    private static int checks;
    private static void check(boolean condition,String message){checks++;if(!condition)throw new AssertionError(message);}
    public static void main(String[] args)throws Exception {
        Path output=Path.of(args[0]),png=Path.of(args[1]);Files.createDirectories(output);
        String hash="0123456789abcdef".repeat(4);
        String encoded=Base64.getEncoder().encodeToString(("{\"textures\":{\"SKIN\":{\"url\":\"https://textures.minecraft.net/texture/"+hash+"\",\"metadata\":{\"model\":\"slim\"}}}}").getBytes(StandardCharsets.UTF_8));
        Property texture=new Property("textures",encoded,"original-signature");
        PropertyMap properties=new PropertyMap(ImmutableMultimap.of("textures",texture));
        UUID npcId=UUID.fromString("bbe3e933-8192-332f-a10d-1e372e979de7");
        GameProfile blank=new GameProfile(npcId,"§a大厅NPC"),textured=new GameProfile(npcId,blank.name(),properties);
        check(PlayerProfiles.actualProfile(npcId,blank,textured)==textured,"cached NPC profile texture should be preserved");
        check(PlayerProfiles.actualProfile(npcId,textured,blank)==textured,"existing authoritative textures must win");
        check(PlayerProfiles.actualProfile(npcId,blank,new GameProfile(npcId,"other",properties))==blank,"do not borrow another name's cached profile");
        UUID changed=UUID.randomUUID();GameProfile aligned=PlayerProfiles.actualProfile(changed,textured,null);
        check(aligned.id().equals(changed)&&aligned.name().equals(textured.name())&&aligned.properties().get("textures").contains(texture),"align real entity identity without losing original properties/signature");
        Map<UUID,ClientboundPlayerInfoUpdatePacket.Entry> entries=new LinkedHashMap<>();
        check(PlayerProfiles.addMissing(entries,npcId,textured,null,false,GameType.SURVIVAL),"NPC missing from Tab must be included");
        check(!PlayerProfiles.addMissing(entries,npcId,blank,null,true,GameType.SURVIVAL)&&entries.size()==1,"existing info must not be duplicated or replaced");
        var entry=entries.get(npcId);
        check(!entry.listed()&&entry.profileId().equals(npcId)&&entry.profile().properties().get("textures").contains(texture),"NPC entry must retain signed profile and stay unlisted");
        check(entry.gameMode()==GameType.SURVIVAL,"missing cached mode must not create invisible spectator");
        var parsed=RecordedSkinCapture.profileTexture(texture);
        check(parsed!=null&&parsed.hash().equals(hash)&&parsed.model().equals("slim"),"read profile skin URL/hash/model without fetching URL");
        String renewed=Base64.getEncoder().encodeToString(("{\"timestamp\":1234,\"textures\":{\"SKIN\":{\"url\":\"https://textures.minecraft.net/texture/"+hash+"\",\"metadata\":{\"model\":\"slim\"}}}}").getBytes(StandardCharsets.UTF_8));
        check(RecordedSkinCapture.profileKey(List.of(texture)).equals(RecordedSkinCapture.profileKey(List.of(new Property("textures",renewed,"renewed-signature")))),"reissued profile timestamp/signature must not invalidate same skin texture");
        check(RecordedSkinCapture.profileTexture(new Property("textures","not-base64"))==null,"invalid texture JSON must not be guessed");
        Path cache=output.resolve("assets/skins");Files.createDirectories(cache);
        Path extensionless=RecordedSkinCapture.cachePath(cache,hash);Files.createDirectories(extensionless.getParent());Files.copy(png,extensionless,StandardCopyOption.REPLACE_EXISTING);
        Path record=output.resolve("recording");RecordedSkinCapture.Writer writer=new RecordedSkinCapture.Writer(record);
        List<String> errors=new ArrayList<>();
        var candidate=new RecordedSkinCapture.Candidate(npcId.toString(),blank.name(),extensionless.toString(),"slim","profile:A");
        writer.write(new RecordedSkinCapture.Batch(List.of(cache.toString()),List.of(candidate),Map.of(npcId.toString(),"profile:A")),errors::add);
        check(writer.count()==1&&writer.errors()==0&&errors.isEmpty(),"real extensionless vanilla cache PNG must be copied");
        check(Files.readString(record.resolve("recorded-skins.json")).contains("\"model\":\"slim\""),"manifest preserves actual arm model");
        var missing=new RecordedSkinCapture.Candidate(npcId.toString(),blank.name(),cache.resolve("not-yet-loaded").toString(),"wide","profile:A");
        writer.write(new RecordedSkinCapture.Batch(List.of(cache.toString()),List.of(missing),Map.of(npcId.toString(),"profile:A")),errors::add);
        check(writer.count()==1&&writer.errors()==0,"missing cache must retain already saved same authoritative skin");
        writer.write(new RecordedSkinCapture.Batch(List.of(cache.toString()),List.of(),Map.of()),errors::add);
        check(writer.count()==1,"departed players and cleared launcher caches retain previously copied skins");
        writer.write(new RecordedSkinCapture.Batch(List.of(cache.toString()),List.of(),Map.of(npcId.toString(),"profile:B")),errors::add);
        check(writer.count()==0&&writer.errors()==0,"missing authoritative cached texture removes obsolete placeholder override without treating a pending download as error");
        check(Files.readString(record.resolve("recorded-skins.json")).contains("\"players\":[]"),"empty manifest must also be published");
        Path forbidden=output.resolve("outside.png");Files.copy(png,forbidden,StandardCopyOption.REPLACE_EXISTING);
        writer.write(new RecordedSkinCapture.Batch(List.of(cache.toString()),List.of(new RecordedSkinCapture.Candidate(npcId.toString(),blank.name(),forbidden.toString(),"wide")),Map.of()),errors::add);
        check(writer.count()==0&&writer.errors()==1,"arbitrary file outside allowed skin roots must be rejected");
        System.out.println("PASS ProfileSkinContractTest: "+checks+" checks");
    }
}
