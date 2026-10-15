import java.io.*;
import java.nio.file.*;
import java.util.*;
import com.replaymod.replaystudio.protocol.*;
import com.replaymod.replaystudio.protocol.packets.PacketConfigRegistries;
import com.replaymod.replaystudio.protocol.packets.PacketPlayerListEntry;
import com.replaymod.replaystudio.protocol.registry.Registries;
import com.replaymod.replaystudio.lib.viaversion.api.protocol.packet.State;
import com.replaymod.replaystudio.lib.viaversion.api.protocol.version.ProtocolVersion;
import com.replaymod.replaystudio.lib.viaversion.protocols.v1_21_7to1_21_9.packet.ClientboundPackets1_21_9;
import com.replaymod.replaystudio.lib.viaversion.protocols.v1_21_7to1_21_9.packet.ClientboundConfigurationPackets1_21_9;
import com.replaymod.lib.com.github.steveice10.opennbt.tag.builtin.*;
import com.replaymod.lib.org.apache.commons.lang3.tuple.Pair;
import com.replaymod.replaystudio.lib.viaversion.api.type.Types;
import com.replaymod.replaystudio.lib.viaversion.api.minecraft.Vector3d;
import com.replaymod.replaystudio.lib.viaversion.api.minecraft.entities.EntityTypes1_21_11;
import io.netty.buffer.Unpooled;

/** Synthetic protocol contract checks. Does not attach to or launch Minecraft. */
public final class ModernAdapterSelfTest extends ModernTestSupport {
    public static void main(String[] args) throws Exception {
        Path dir = Paths.get(args[0]); Files.createDirectories(dir);
        Files.deleteIfExists(dir.resolve("recorded-skins.json"));
        Files.deleteIfExists(dir.resolve("runtime-configurations.bin"));
        Files.writeString(dir.resolve("replay-player.properties"), "sourcePlayerId=42\nplayerUuid=00000000-0000-0000-0000-000000000001\nplayerNameBase64=VGVzdA==\ngametype=0\nsourceProtocol=773\n");
        Files.writeString(dir.resolve("capture.properties"), "sourceProtocol=773\nlocalHotbarSlots=false\n");
        Registries registries = new Registries(); registries.registriesMap = new LinkedHashMap<>();
        CompoundTag dimension = new CompoundTag(); dimension.put("min_y", new IntTag(-64)); dimension.put("height", new IntTag(384));
        registries.registriesMap.put("minecraft:dimension_type", List.of(Pair.of("minecraft:overworld", dimension)));
        CompoundTag biome = new CompoundTag(); biome.put("effects", new CompoundTag());
        registries.registriesMap.put("minecraft:worldgen/biome", List.of(Pair.of("minecraft:plains", biome)));
        var configRegistry = PacketTypeRegistry.get(ProtocolVersion.getProtocol(773), State.CONFIGURATION);
        try (DataOutputStream out = new DataOutputStream(Files.newOutputStream(dir.resolve("snapshot-config.bin")))) {
            for (Packet p : PacketConfigRegistries.write(configRegistry, registries)) {
                ByteArrayOutputStream b = new ByteArrayOutputStream(); writeVarInt(b, p.getId()); b.write(bytes(p.getBuf())); p.release();
                byte[] raw = b.toByteArray(); out.writeInt(raw.length); out.write(raw);
            }
            out.writeInt(1); out.writeByte(ClientboundConfigurationPackets1_21_9.FINISH_CONFIGURATION.getId());
        }
        ByteArrayOutputStream join = new ByteArrayOutputStream(); DataOutputStream j = new DataOutputStream(join);
        writeVarInt(join, ClientboundPackets1_21_9.LOGIN.getId()); j.writeInt(42); j.writeBoolean(false);
        writeVarInt(join, 1); string(join, "minecraft:overworld"); writeVarInt(join, 20); writeVarInt(join, 8); writeVarInt(join, 8);
        j.writeBoolean(false); j.writeBoolean(true); j.writeBoolean(false); writeVarInt(join, 0); string(join, "minecraft:overworld");
        j.writeLong(0); j.writeByte(0); j.writeByte(-1); j.writeBoolean(false); j.writeBoolean(false); j.writeBoolean(false);
        writeVarInt(join, 0); writeVarInt(join, 63); j.writeBoolean(false);
        Files.write(dir.resolve("snapshot-packets.bin"), join.toByteArray());
        Files.write(dir.resolve("snapshot-chunks.bin"), new byte[0]);
        Files.write(dir.resolve("snapshot-entities.bin"), new byte[0]);
        Files.write(dir.resolve("snapshot-world.bin"), new byte[0]);
        try (DataOutputStream out = new DataOutputStream(Files.newOutputStream(dir.resolve("runtime-packets.bin")))) {
            out.writeLong(0); out.writeInt(1); out.writeByte(0); // bundle marker must be excluded
            byte[] animation = {(byte)ClientboundPackets1_21_9.ANIMATE.getId(), 42, 0};
            out.writeLong(1); out.writeInt(animation.length); out.write(animation);
            ByteArrayOutputStream respawn = new ByteArrayOutputStream(); DataOutputStream r = new DataOutputStream(respawn);
            writeVarInt(respawn, ClientboundPackets1_21_9.RESPAWN.getId()); writeVarInt(respawn, 0);
            string(respawn, "minecraft:the_nether"); r.writeLong(0); r.writeByte(1); r.writeByte(0);
            r.writeBoolean(false); r.writeBoolean(false); r.writeBoolean(false); writeVarInt(respawn, 0); writeVarInt(respawn, 63); r.writeByte(3);
            byte[] raw = respawn.toByteArray(); out.writeLong(2); out.writeInt(raw.length); out.write(raw);
        }
        ModernFlashbackProtocolAdapter.main(new String[]{dir.toString()});
        try (DataInputStream in = new DataInputStream(Files.newInputStream(dir.resolve("converted-packets.bin")))) {
            check(in.readLong() == 1, "runtime tick preserved and bundle skipped");
            byte[] payload = in.readNBytes(in.readInt()); Cursor c = new Cursor(payload);
            check(c.vi() == 2 && c.vi() == ~42 && c.u8() == 0, "local animation entity id remapped");
            check(in.readLong() == 2, "respawn tick preserved");
            byte[] respawn = in.readNBytes(in.readInt()); Cursor rc = new Cursor(respawn);
            check(rc.vi() == 80, "respawn target packet id"); rc.vi();
            check(ModernFlashbackProtocolAdapter.readString(rc).equals("minecraft:the_nether"), "respawn destination retained");
            rc.skip(8); check(rc.u8() == 3, "respawn spectator mode rewritten");
            check(in.read() == -1, "only supported runtime packets emitted");
        }
        try (DataInputStream in = new DataInputStream(Files.newInputStream(dir.resolve("converted-timeline.bin")))) {
            check(in.readLong() == 1 && in.readInt() == 0, "animation timeline entry"); in.skipNBytes(in.readInt());
            check(in.readLong() == 2 && in.readInt() == 0, "respawn timeline entry"); in.skipNBytes(in.readInt());
            check(in.readLong() == 2 && in.readInt() == 2, "local player marker follows respawn");
            Cursor marker = new Cursor(in.readNBytes(in.readInt()));
            check(marker.u8() == 1 && ModernFlashbackProtocolAdapter.readString(marker).equals("minecraft:the_nether"), "marker preserves original gametype and destination");
            check(in.read() == -1, "exactly one respawn marker");
        }
        try (DataInputStream in = new DataInputStream(Files.newInputStream(dir.resolve("converted-game-snapshot.bin")))) {
            in.readInt(); byte[] payload = in.readNBytes(in.readInt()); Cursor c = new Cursor(payload);
            check(c.vi() == 48 && c.intValue() == ~42, "local Join Game id complemented");
        }
        ByteArrayOutputStream tags = new ByteArrayOutputStream(); writeVarInt(tags, 13); writeVarInt(tags, 4);
        string(tags, "neoforge:attachment_types"); writeVarInt(tags, 0);
        string(tags, "minecraft:schedule"); writeVarInt(tags, 0);
        string(tags, "minecraft:item"); writeVarInt(tags, 1); string(tags, "neoforge:foods"); writeVarInt(tags, 2); writeVarInt(tags, 1); writeVarInt(tags, 2);
        string(tags, "mod:custom_registry"); writeVarInt(tags, 1); string(tags, "mod:custom_tag"); writeVarInt(tags, 1); writeVarInt(tags, 42);
        byte[] filteredTags = ModernFlashbackProtocolAdapter.vanillaRegistryTags(tags.toByteArray(), "test");
        Cursor tc = new Cursor(filteredTags);
        check(tc.vi() == 13 && tc.vi() == 1 && ModernFlashbackProtocolAdapter.readString(tc).equals("minecraft:item"), "only vanilla registry groups retained");
        check(tc.vi() == 1 && ModernFlashbackProtocolAdapter.readString(tc).equals("neoforge:foods") && tc.vi() == 2 && tc.vi() == 1 && tc.vi() == 2
            && tc.pos == filteredTags.length, "mod tag names on vanilla registries and their entries preserved");
        check(ModernFlashbackProtocolAdapter.diagnostics.get("test.customRegistryTagGroupsDropped") == 2, "unknown registry drop count includes empty groups");
        check(ModernFlashbackProtocolAdapter.diagnostics.get("test.emptyRegistryTagGroupsDropped") == 1, "empty vanilla registry groups match vanilla network serialization");
        ByteArrayOutputStream damage = new ByteArrayOutputStream();
        writeVarInt(damage, 25); writeVarInt(damage, 42); writeVarInt(damage, 1);
        writeVarInt(damage, 43); writeVarInt(damage, 0); damage.write(0);
        Cursor damageResult = new Cursor(ModernFlashbackProtocolAdapter.modernReplayPacket(damage.toByteArray(), false));
        check(damageResult.vi() == 25 && damageResult.vi() == ~42 && damageResult.vi() == 1
            && damageResult.vi() == (~42) + 1 && damageResult.vi() == 0, "damage causes rewritten without changing absent-id sentinel");
        var spawn = Unpooled.buffer();
        try {
            Types.VAR_INT.writePrimitive(spawn, 1); Types.VAR_INT.writePrimitive(spawn, 99);
            spawn.writeLong(0); spawn.writeLong(2); Types.VAR_INT.writePrimitive(spawn, EntityTypes1_21_11.ARROW.getId());
            spawn.writeDouble(1); spawn.writeDouble(2); spawn.writeDouble(3);
            Types.LOW_PRECISION_VECTOR.write(spawn, new Vector3d(0.1, 0.2, 0.3));
            spawn.writeByte(1); spawn.writeByte(2); spawn.writeByte(3); Types.VAR_INT.writePrimitive(spawn, 42);
            byte[] rewritten = ModernFlashbackProtocolAdapter.modernReplayPacket(bytes(spawn), false);
            Cursor c = new Cursor(rewritten); c.vi(); check(c.vi() == 99, "nonlocal projectile id preserved"); c.skip(16); c.vi(); c.skip(24);
            var movement = Unpooled.wrappedBuffer(rewritten);
            try { movement.readerIndex(c.pos); Types.LOW_PRECISION_VECTOR.read(movement); c.pos = movement.readerIndex(); }
            finally { movement.release(); }
            c.skip(3); check(c.vi() == ~42, "projectile owner remapped after variable-length movement");
        } finally { spawn.release(); }
        testTransfers(dir, join.toByteArray());
        // NPCs removed from tab before capture still need their real profile
        // before spawning. Removed profiles may be needed again after a seek
        // or later spawn, and their signed texture property must survive.
        UUID npc = UUID.fromString("bbe3e933-8192-332f-a10d-1e372e979de7");
        String npcName = "§2§k§e§e§o§f§c§m";
        Files.writeString(dir.resolve("recorded-skins.json"), "{\"version\":1,\"players\":[{\"uuid\":\"" + npc + "\",\"name\":\"" + npcName + "\"}]}");
        ModernFlashbackProtocolAdapter.loadRecordedProfileNames(dir.resolve("recorded-skins.json"));
        byte[] npcSpawn = playerSpawnHeader(npc);
        byte[] npcInfo = ModernFlashbackProtocolAdapter.playerInfoBeforeSpawn(npcSpawn, "test");
        check(npcInfo != null, "missing NPC profile is supplied from recorded identity");
        PacketPlayerListEntry entry = playerEntry(npcInfo);
        check(entry.getUuid().equals(npc) && entry.getName().equals(npcName) && !entry.isListed(), "true NPC name/UUID retained and not added to tab");
        check(ModernFlashbackProtocolAdapter.playerInfoBeforeSpawn(npcSpawn, "test") == null, "known NPC profile is never replaced");
        check(ModernFlashbackProtocolAdapter.playerInfoBeforeSpawn(playerSpawnHeader(new UUID(0, 999)), "test") == null, "no profile is fabricated without identity evidence");
        ByteArrayOutputStream signedInfo = new ByteArrayOutputStream(); DataOutputStream si = new DataOutputStream(signedInfo);
        writeVarInt(signedInfo, 68); si.writeByte(1); writeVarInt(signedInfo, 1);
        si.writeLong(npc.getMostSignificantBits()); si.writeLong(npc.getLeastSignificantBits()); string(signedInfo, npcName);
        writeVarInt(signedInfo, 1); string(signedInfo, "textures"); string(signedInfo, "original-texture"); si.writeBoolean(true); string(signedInfo, "original-signature");
        ModernFlashbackProtocolAdapter.playerInfoBeforeSpawn(signedInfo.toByteArray(), "test");
        ByteArrayOutputStream removal = new ByteArrayOutputStream(); DataOutputStream rm = new DataOutputStream(removal);
        writeVarInt(removal, 67); writeVarInt(removal, 1); rm.writeLong(npc.getMostSignificantBits()); rm.writeLong(npc.getLeastSignificantBits());
        ModernFlashbackProtocolAdapter.playerInfoBeforeSpawn(removal.toByteArray(), "test");
        byte[] restored = ModernFlashbackProtocolAdapter.playerInfoBeforeSpawn(npcSpawn, "test");
        check(restored != null, "removed profile restored immediately before later spawn");
        Cursor restoredFields = new Cursor(restored); restoredFields.vi(); restoredFields.u8(); restoredFields.vi(); restoredFields.skip(16);
        ModernFlashbackProtocolAdapter.readString(restoredFields);
        check(restoredFields.vi() == 1 && ModernFlashbackProtocolAdapter.readString(restoredFields).equals("textures")
            && ModernFlashbackProtocolAdapter.readString(restoredFields).equals("original-texture") && restoredFields.u8() == 1
            && ModernFlashbackProtocolAdapter.readString(restoredFields).equals("original-signature"), "server texture property and signature retained across removal");
        Files.writeString(dir.resolve("recorded-skins.json"), "{\"version\":1,\"players\":[{\"uuid\":\"" + npc + "\",\"name\":\"Valid\"},{\"uuid\":\"not-a-uuid\"}]}");
        ModernFlashbackProtocolAdapter.loadRecordedProfileNames(dir.resolve("recorded-skins.json"));
        check(ModernFlashbackProtocolAdapter.recordedProfileNames.isEmpty(), "invalid optional manifest cannot supply partial/untrusted identity evidence");
        check(ModernFlashbackProtocolAdapter.diagnostics.get("recordedProfileManifestInvalid") == 1, "invalid optional skin manifest reports a warning instead of aborting conversion");
        String validIdentity = "{\"uuid\":\"" + npc + "\",\"name\":\"Valid\"}";
        List<String> invalidManifests = List.of(
            "{\"version\":1.5,\"players\":[" + validIdentity + "]}",
            "{\"version\":\"1\",\"players\":[" + validIdentity + "]}",
            "{\"version\":1,\"players\":[{\"uuid\":\"" + npc + "\",\"name\":123}]}",
            "{\"version\":1,\"players\":[{\"uuid\":\"1-1-1-1-1\",\"name\":\"Valid\"}]}",
            "{\"version\":1,\"players\":[" + validIdentity + "," + validIdentity + "]}");
        for (String invalidManifest : invalidManifests) {
            int warnings = ModernFlashbackProtocolAdapter.diagnostics.get("recordedProfileManifestInvalid");
            Files.writeString(dir.resolve("recorded-skins.json"), invalidManifest);
            ModernFlashbackProtocolAdapter.loadRecordedProfileNames(dir.resolve("recorded-skins.json"));
            check(ModernFlashbackProtocolAdapter.recordedProfileNames.isEmpty()
                && ModernFlashbackProtocolAdapter.diagnostics.get("recordedProfileManifestInvalid") == warnings + 1,
                "wrong JSON identity types, fractional versions, short UUIDs and duplicate identities are wholly rejected");
        }
        // A malformed retry must fail and must not leave a stale success marker.
        Files.write(dir.resolve("runtime-packets.bin"), new byte[]{0, 0, 0});
        boolean rejected = false;
        try { ModernFlashbackProtocolAdapter.main(new String[]{dir.toString()}); } catch (IOException expected) { rejected = true; }
        check(rejected, "truncated runtime packet rejected");
        check(!Files.exists(dir.resolve("converted.properties")), "failed conversion has no success marker");
        System.out.println("PASS: modern 773->774 configuration/join/runtime conversion, NPC profile recovery and lifetime/signature preservation, bundle filtering, local id/damage/projectile owner mapping, malformed-input rejection");
    }

    static byte[] playerSpawnHeader(UUID uuid) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream(); DataOutputStream data = new DataOutputStream(out);
        writeVarInt(out, 1); writeVarInt(out, 612); data.writeLong(uuid.getMostSignificantBits()); data.writeLong(uuid.getLeastSignificantBits());
        writeVarInt(out, EntityTypes1_21_11.PLAYER.getId()); return out.toByteArray();
    }

    static void testTransfers(Path dir, byte[] initialJoin) throws Exception {
        List<byte[]> configuration = new ArrayList<>();
        ModernFlashbackProtocolAdapter.forEachPacket(dir.resolve("snapshot-config.bin"), true, configuration::add);
        byte[] start = {(byte)ClientboundPackets1_21_9.START_CONFIGURATION.getId()};
        byte[] join77 = initialJoin.clone(), join99 = initialJoin.clone();
        java.nio.ByteBuffer.wrap(join77, 1, 4).putInt(77); java.nio.ByteBuffer.wrap(join99, 1, 4).putInt(99);
        try (DataOutputStream out = new DataOutputStream(Files.newOutputStream(dir.resolve("runtime-packets.bin")))) {
            runtimeFrame(out, 0, new byte[]{(byte)ClientboundPackets1_21_9.ANIMATE.getId(), 42, 0});
            runtimeFrame(out, 2, start); runtimeFrame(out, 3, join77);
            runtimeFrame(out, 3, new byte[]{(byte)ClientboundPackets1_21_9.ANIMATE.getId(), 77, 0});
            runtimeFrame(out, 6, start); runtimeFrame(out, 7, join99);
            runtimeFrame(out, 8, new byte[]{(byte)ClientboundPackets1_21_9.ANIMATE.getId(), 99, 0});
            runtimeFrame(out, 9, start); // Stopped during a third transfer.
        }
        writeConfigurations(dir, configuration, 2);
        ModernFlashbackProtocolAdapter.main(new String[]{dir.toString()});
        int joins = 0, markers = 0, animations = 0, pendingConfiguration = 0;
        try (DataInputStream in = new DataInputStream(Files.newInputStream(dir.resolve("converted-timeline.bin")))) {
            while (in.available() > 0) {
                long tick = in.readLong(); int kind = in.readInt(); byte[] packet = in.readNBytes(in.readInt());
                Cursor cursor = new Cursor(packet);
                if (kind == 1) { check(tick == 3 || tick == 7, "configuration uses exact destination LOGIN tick"); pendingConfiguration++; }
                else if (kind == 2) { check(joins > markers, "local marker follows its new LOGIN"); markers++; }
                else {
                    int id = cursor.vi();
                    check(id != ClientboundPackets1_21_9.START_CONFIGURATION.getId(), "connection handshake is not sent as a replay game packet");
                    if (id == 48) {
                        check(pendingConfiguration >= 2 && cursor.intValue() == ~42, "complete new configuration precedes LOGIN with fixed replay local id");
                        pendingConfiguration = 0; joins++;
                    } else if (id == 2) {
                        check(cursor.vi() == ~42, "changed source local ids map to one stable replay id"); animations++;
                    }
                }
            }
        }
        check(joins == 2 && markers == 2 && animations == 3 && pendingConfiguration == 0, "two complete transfers retain event order and all local animation packets");
        Properties props = ModernFlashbackProtocolAdapter.readProperties(dir.resolve("converted.properties"), true);
        check(props.getProperty("worldTransitions").equals("2") && props.getProperty("incompleteWorldTransitions").equals("1"), "unfinished final transfer is reported while retaining the playable prefix");
        writeConfigurations(dir, configuration, 3); // Same tick is insufficient: the ordinal must also match.
        boolean rejected = false;
        try { ModernFlashbackProtocolAdapter.main(new String[]{dir.toString()}); } catch (IOException expected) { rejected = true; }
        check(rejected && !Files.exists(dir.resolve("converted.properties")), "misbound configuration cannot silently corrupt a replay");
        Files.delete(dir.resolve("runtime-configurations.bin"));
    }

    static void runtimeFrame(DataOutputStream out, long tick, byte[] packet) throws IOException {
        out.writeLong(tick); out.writeInt(packet.length); out.write(packet);
    }

    static void writeConfigurations(Path dir, List<byte[]> configuration, long firstIndex) throws IOException {
        try (DataOutputStream out = new DataOutputStream(Files.newOutputStream(dir.resolve("runtime-configurations.bin")))) {
            for (long[] transition : List.of(new long[]{3, firstIndex}, new long[]{7, 5})) {
                out.writeLong(transition[0]); out.writeLong(transition[1]); out.writeInt(configuration.size());
                for (byte[] packet : configuration) { out.writeInt(packet.length); out.write(packet); }
            }
        }
    }

    static PacketPlayerListEntry playerEntry(byte[] raw) throws IOException {
        Cursor cursor = new Cursor(raw); int id = cursor.vi();
        Packet packet = new Packet(ModernFlashbackProtocolAdapter.TARGET_PLAY, id, Unpooled.wrappedBuffer(raw, cursor.pos, raw.length - cursor.pos));
        try { return PacketPlayerListEntry.read(packet).get(0); } finally { packet.release(); }
    }
}

class ModernTestSupport extends FlashbackProtocolAdapter {
    static void string(OutputStream out, String s) throws IOException { byte[] b = s.getBytes(java.nio.charset.StandardCharsets.UTF_8); writeVarInt(out, b.length); out.write(b); }
    static void check(boolean yes, String message) { if (!yes) throw new AssertionError(message); }
}
