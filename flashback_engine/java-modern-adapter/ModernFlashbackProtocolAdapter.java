import com.replaymod.replaystudio.viaversion.ViaVersionPacketConverter;
import com.replaymod.replaystudio.lib.viaversion.api.protocol.packet.State;
import com.replaymod.replaystudio.lib.viaversion.api.protocol.version.ProtocolVersion;
import com.replaymod.replaystudio.lib.viaversion.api.minecraft.entities.EntityTypes1_21_11;
import com.replaymod.replaystudio.lib.viaversion.api.type.Types;
import com.replaymod.replaystudio.protocol.Packet;
import com.replaymod.replaystudio.protocol.PacketTypeRegistry;
import com.replaymod.replaystudio.protocol.packets.PacketLoginSuccess;
import com.replaymod.replaystudio.protocol.packets.PacketPlayerListEntry;
import com.replaymod.replaystudio.protocol.packets.PacketPlayerListEntry.Action;
import com.replaymod.replaystudio.lib.viaversion.libs.gson.*;
import com.replaymod.replaystudio.lib.viaversion.protocols.v1_21_7to1_21_9.packet.ClientboundPackets1_21_9;
import com.replaymod.replaystudio.lib.viaversion.protocols.v1_21_7to1_21_9.packet.ClientboundConfigurationPackets1_21_9;
import com.replaymod.replaystudio.lib.viaversion.protocols.v1_21_9to1_21_11.packet.ClientboundPackets1_21_11;
import io.netty.buffer.ByteBuf;
import io.netty.buffer.Unpooled;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

/** 1.21.10 (773) capture -> Flashback 0.39.10 / 1.21.11 (774).
 * Kept separate from the immutable, bundled 1.20.1 adapter.
 * Input configuration and snapshot sidecars: big-endian int32 size + packet.
 * Runtime sidecar: big-endian int64 tick + int32 size + packet.
 * Output framing is identical to FlashbackProtocolAdapter v5.
 */
public final class ModernFlashbackProtocolAdapter extends FlashbackProtocolAdapter {
    static final int SOURCE = 773, TARGET = 774, MAX_PACKET = 64 * 1024 * 1024;
    static final int JOIN = ClientboundPackets1_21_9.LOGIN.getId();
    static final int TARGET_JOIN = ClientboundPackets1_21_11.LOGIN.getId();
    static final PacketTypeRegistry TARGET_PLAY = PacketTypeRegistry.get(ProtocolVersion.getProtocol(TARGET), State.PLAY);
    static final Map<UUID, String> recordedProfileNames = new HashMap<>();
    static final Map<UUID, PacketPlayerListEntry> lastProfiles = new HashMap<>();
    static final Set<UUID> presentProfiles = new HashSet<>();

    @FunctionalInterface interface PacketConsumer { void accept(byte[] packet) throws Exception; }
    record ConfigurationEvent(long tick, long beforePacketIndex, List<byte[]> packets) {}

    static Properties readProperties(Path path, boolean required) throws IOException {
        Properties p = new Properties();
        if (!required && !Files.exists(path)) return p;
        try (InputStream in = Files.newInputStream(path)) { p.load(in); }
        return p;
    }

    static byte[] readPacket(DataInputStream in, int size, String file) throws IOException {
        if (size <= 0 || size > MAX_PACKET) throw new IOException("Invalid packet length in " + file + ": " + size);
        byte[] packet = in.readNBytes(size);
        if (packet.length != size) throw new EOFException("Truncated packet in " + file);
        return packet;
    }

    static void forEachPacket(Path path, boolean required, PacketConsumer consumer) throws Exception {
        if (!required && !Files.exists(path)) return;
        try (DataInputStream in = new DataInputStream(new BufferedInputStream(Files.newInputStream(path)))) {
            while (in.available() > 0) consumer.accept(readPacket(in, in.readInt(), path.getFileName().toString()));
        }
    }

    static byte[] modernLogin() throws IOException {
        PacketTypeRegistry registry = PacketTypeRegistry.get(ProtocolVersion.getProtocol(SOURCE), State.LOGIN);
        Packet packet = new PacketLoginSuccess(playerUuid, playerName, List.of(), false, new UUID(0, 0)).write(registry);
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            writeVarInt(out, packet.getId()); out.write(bytes(packet.getBuf())); return out.toByteArray();
        } finally { packet.release(); }
    }

    static String readString(Cursor cursor) throws IOException {
        int length = cursor.vi();
        if (length < 0 || length > 32767 * 4) throw new IOException("Invalid protocol string length");
        int start = cursor.pos; cursor.skip(length);
        return new String(cursor.data, start, length, StandardCharsets.UTF_8);
    }

    static UUID readUuid(Cursor cursor) throws IOException {
        int start = cursor.pos; cursor.skip(16);
        var buffer = java.nio.ByteBuffer.wrap(cursor.data, start, 16);
        return new UUID(buffer.getLong(), buffer.getLong());
    }

    /** Old captures already contain the real profile UUID/name in the skin
     * manifest, even when an NPC was removed from the server's tab list before
     * recording. This is identity evidence only: never invent a profile name,
     * replace a server-sent profile, or treat an image as a signed texture. */
    static void loadRecordedProfileNames(Path path) throws IOException {
        recordedProfileNames.clear(); lastProfiles.clear(); presentProfiles.clear();
        if (!Files.exists(path)) return;
        try {
            if (Files.size(path) > 4 * 1024 * 1024) throw new IOException("Recorded skin manifest is too large");
            try (Reader reader = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
            JsonObject root = JsonParser.parseReader(reader).getAsJsonObject();
            JsonElement version = root.get("version");
            if (version == null || !version.isJsonPrimitive() || !version.getAsJsonPrimitive().isNumber()
                    || !version.getAsString().equals("1")) throw new IOException("Unsupported recorded skin manifest version");
            JsonArray players = root.getAsJsonArray("players");
            if (players.size() > 65536) throw new IOException("Too many recorded player identities");
            for (JsonElement value : players) {
                JsonObject player = value.getAsJsonObject();
                JsonElement uuidValue = player.get("uuid"), nameValue = player.get("name");
                if (uuidValue == null || !uuidValue.isJsonPrimitive() || !uuidValue.getAsJsonPrimitive().isString()
                        || nameValue == null || !nameValue.isJsonPrimitive() || !nameValue.getAsJsonPrimitive().isString())
                    throw new IOException("Recorded profile identity fields must be strings");
                String uuidText = uuidValue.getAsString(); UUID uuid = UUID.fromString(uuidText);
                if (!uuid.toString().equals(uuidText)) throw new IOException("Recorded profile UUID must be canonical");
                String name = nameValue.getAsString();
                if (name.isBlank() || name.length() > 16) throw new IOException("Invalid recorded player profile name");
                String old = recordedProfileNames.putIfAbsent(uuid, name);
                if (old != null) throw new IOException("Duplicate recorded player profile UUID");
            }
            }
        } catch (IOException | RuntimeException invalid) {
            // Skins are optional. Reject the whole identity source instead of
            // accepting a partial manifest or blocking otherwise valid replay
            // data; unresolved player spawns get their own diagnostic count.
            recordedProfileNames.clear(); count("recordedProfileManifestInvalid");
            System.err.println("Optional recorded-skins.json identity manifest is invalid; profile recovery from it was disabled");
        }
    }

    static byte[] encodePacket(Packet packet) throws IOException {
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            writeVarInt(out, packet.getId()); out.write(bytes(packet.getBuf())); return out.toByteArray();
        } finally { packet.release(); }
    }

    static byte[] recordedPlayerInfo(UUID uuid, String name) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream(); DataOutputStream data = new DataOutputStream(out);
        writeVarInt(out, ClientboundPackets1_21_11.PLAYER_INFO_UPDATE.getId());
        data.writeByte(1 | 4 | 8 | 128); // ADD_PLAYER / game mode / listed / hat
        writeVarInt(out, 1); data.writeLong(uuid.getMostSignificantBits()); data.writeLong(uuid.getLeastSignificantBits());
        byte[] encoded = name.getBytes(StandardCharsets.UTF_8); writeVarInt(out, encoded.length); out.write(encoded);
        writeVarInt(out, 0); // No texture properties were captured in this identity manifest.
        writeVarInt(out, 0); data.writeBoolean(false); data.writeBoolean(true);
        return out.toByteArray();
    }

    static void observePlayerInfo(byte[] raw) throws IOException {
        Cursor cursor = new Cursor(raw); int id = cursor.vi();
        if (id != ClientboundPackets1_21_11.PLAYER_INFO_UPDATE.getId()
                && id != ClientboundPackets1_21_11.PLAYER_INFO_REMOVE.getId()) return;
        Packet packet = new Packet(TARGET_PLAY, id, Unpooled.wrappedBuffer(raw, cursor.pos, raw.length - cursor.pos));
        try {
            Set<Action> actions = PacketPlayerListEntry.getActions(packet);
            for (PacketPlayerListEntry next : PacketPlayerListEntry.read(packet)) {
                UUID uuid = next.getUuid();
                if (actions.contains(Action.REMOVE)) { presentProfiles.remove(uuid); continue; }
                PacketPlayerListEntry previous = lastProfiles.get(uuid);
                if (actions.contains(Action.ADD)) { previous = next; presentProfiles.add(uuid); }
                if (previous == null) continue;
                if (actions.contains(Action.GAMEMODE)) previous = PacketPlayerListEntry.updateGamemode(previous, next.getGamemode());
                if (actions.contains(Action.LISTED)) previous = PacketPlayerListEntry.updateListed(previous, next.isListed());
                if (actions.contains(Action.LATENCY)) previous = PacketPlayerListEntry.updateLatency(previous, next.getLatency());
                if (actions.contains(Action.DISPLAY_NAME)) previous = PacketPlayerListEntry.updateDisplayName(previous, next.getDisplayName());
                if (actions.contains(Action.LIST_ORDER)) previous = PacketPlayerListEntry.updateListOrder(previous, next.getListOrder());
                if (actions.contains(Action.SHOW_HAT)) previous = PacketPlayerListEntry.updateShowHat(previous, next.getShowHat());
                lastProfiles.put(uuid, previous);
            }
        } finally { packet.release(); }
    }

    /** Ensure ADD_PLAYER precedes every player spawn. Keep ordinary packets
     * byte-for-byte and retain server texture properties across a remove then
     * respawn. The C# writer separately delays removals while an NPC is alive. */
    static byte[] playerInfoBeforeSpawn(byte[] raw, String stage) throws IOException {
        observePlayerInfo(raw);
        Cursor cursor = new Cursor(raw);
        if (cursor.vi() != ClientboundPackets1_21_11.ADD_ENTITY.getId()) return null;
        cursor.vi(); UUID uuid = readUuid(cursor);
        if (cursor.vi() != EntityTypes1_21_11.PLAYER.getId()) return null;
        count(stage + ".playerSpawns");
        if (presentProfiles.contains(uuid)) return null;
        byte[] prefix;
        PacketPlayerListEntry previous = lastProfiles.get(uuid);
        if (previous != null) {
            prefix = encodePacket(PacketPlayerListEntry.write(TARGET_PLAY,
                EnumSet.of(Action.ADD, Action.GAMEMODE, Action.LISTED, Action.LATENCY, Action.DISPLAY_NAME, Action.LIST_ORDER, Action.SHOW_HAT),
                PacketPlayerListEntry.updateListed(previous, false)));
            count(stage + ".playerProfilesRestored");
        } else {
            String name = recordedProfileNames.get(uuid);
            if (name == null) { count(stage + ".playerProfilesMissing"); return null; }
            prefix = recordedPlayerInfo(uuid, name);
            count(stage + ".playerProfilesRecoveredFromManifest");
        }
        observePlayerInfo(prefix);
        return prefix;
    }

    /** This wire layout is shared by 773 and 774. Preserve every registry id
     * and the complete spawn-info tail; change only player identity/mode. */
    static byte[] modernJoin(byte[] raw, boolean rewrite) throws IOException {
        Cursor cursor = new Cursor(raw);
        int packetId = cursor.vi();
        if (packetId != (rewrite ? TARGET_JOIN : JOIN)) throw new IOException("Snapshot is not Join Game: " + packetId);
        int entityOffset = cursor.pos, entityId = cursor.intValue();
        if (entityId != sourcePlayerId) throw new IOException("Join Game player id " + entityId + " does not match captured identity " + sourcePlayerId);
        cursor.skip(1); // hardcore
        int dimensions = cursor.vi();
        if (dimensions < 1 || dimensions > 65536) throw new IOException("Invalid Join Game dimension count");
        for (int i = 0; i < dimensions; i++) readString(cursor);
        cursor.vi(); cursor.vi(); cursor.vi(); // player/view/simulation limits
        cursor.skip(3); // reduced debug / death screen / limited crafting
        cursor.vi(); // dimension type holder
        String dimension = readString(cursor);
        if (dimension.isBlank()) throw new IOException("Missing Join Game dimension");
        currentDimension = dimension;
        cursor.skip(8); // world seed
        int modeOffset = cursor.pos, mode = cursor.u8();
        if (mode > 3) throw new IOException("Invalid Join Game game mode: " + mode);
        playerGametype = mode;
        if (!rewrite) return raw;
        byte[] result = raw.clone(); int id = localReplayId();
        result[entityOffset] = (byte)(id >>> 24); result[entityOffset + 1] = (byte)(id >>> 16);
        result[entityOffset + 2] = (byte)(id >>> 8); result[entityOffset + 3] = (byte)id;
        result[modeOffset] = 3;
        return result;
    }

    /** Respawn carries CommonPlayerSpawnInfo but no new entity identity or
     * registry configuration. Preserve its data-to-keep flags and mark the
     * new world so the C# writer resumes movement from a destination sample. */
    static byte[] modernRespawn(byte[] raw) throws IOException {
        Cursor cursor = new Cursor(raw);
        if (cursor.vi() != ClientboundPackets1_21_11.RESPAWN.getId()) throw new IOException("Not a target Respawn packet");
        cursor.vi(); currentDimension = readString(cursor); cursor.skip(8);
        int modeOffset = cursor.pos; playerGametype = cursor.u8();
        if (currentDimension.isBlank() || playerGametype > 3) throw new IOException("Invalid Respawn world/game mode");
        byte[] result = raw.clone(); result[modeOffset] = 3;
        return result;
    }

    /** Registry tag groups refer to a registry, not merely a namespaced tag.
     * Vanilla/Fabric playback cannot look up NeoForge-only registries, even
     * when their tag lists are empty. Match the target's vanilla
     * TagNetworkSerialization: empty registry payloads are never sent.
     * Preserve every nonempty minecraft registry group and every tag within
     * it (including mod-namespaced tags on vanilla items).
     * Run before Via so its normal item/block/entity tag-id conversion remains
     * intact. Also apply to play-state tag updates after conversion. */
    static byte[] vanillaRegistryTags(byte[] raw, String stage) throws IOException {
        Cursor cursor = new Cursor(raw); int packetId = cursor.vi(), groups = cursor.vi();
        if (groups < 0 || groups > raw.length) throw new IOException("Invalid tag registry count");
        ByteArrayOutputStream kept = new ByteArrayOutputStream(); int remaining = 0;
        for (int group = 0; group < groups; group++) {
            int start = cursor.pos; String registry = readString(cursor);
            if (registry.isBlank()) throw new IOException("Missing tag registry name");
            int tags = cursor.vi();
            if (tags < 0 || tags > raw.length - cursor.pos) throw new IOException("Invalid registry tag count");
            for (int tag = 0; tag < tags; tag++) {
                readString(cursor); int entries = cursor.vi();
                if (entries < 0 || entries > raw.length - cursor.pos) throw new IOException("Invalid tag entry count");
                for (int entry = 0; entry < entries; entry++) cursor.vi();
            }
            if (!(registry.startsWith("minecraft:") || !registry.contains(":"))) {
                count(stage + ".customRegistryTagGroupsDropped");
                count(stage + ".filteredRegistry." + registry);
                diagnostics.merge(stage + ".customRegistryTagsDropped", tags, Integer::sum);
            } else if (tags == 0) {
                // The source snapshots RegistryAccess wholesale; many empty
                // static registries do not even exist in the target version.
                // 1.21.11 TagNetworkSerialization filters NetworkPayload.isEmpty
                // before putting a registry into ClientboundUpdateTagsPacket.
                count(stage + ".emptyRegistryTagGroupsDropped");
            } else {
                kept.write(raw, start, cursor.pos - start); remaining++;
            }
        }
        if (cursor.pos != raw.length) throw new IOException("Trailing data in registry tag packet");
        if (remaining == groups) return raw;
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        writeVarInt(out, packetId); writeVarInt(out, remaining); kept.writeTo(out); return out.toByteArray();
    }

    /** v5 already handles all normal target identity fields. The old source
     * pass covered projectile owners and damage causes; modern captures need
     * those fields handled here, using 774's low-precision movement codec. */
    static byte[] modernReplayPacket(byte[] packet, boolean localHotbar) throws Exception {
        if (varInt(packet) == ClientboundPackets1_21_11.UPDATE_TAGS.getId()) packet = vanillaRegistryTags(packet, "play");
        byte[] replay = replayPacket(packet, localHotbar);
        if (replay == null) return null;
        EntityIdEditor editor = new EntityIdEditor(replay); int id = editor.cursor.vi();
        if (id == ClientboundPackets1_21_11.ADD_ENTITY.getId()) {
            editor.cursor.vi(); editor.cursor.skip(16);
            int type = editor.cursor.vi();
            editor.cursor.skip(24);
            ByteBuf movement = Unpooled.wrappedBuffer(replay);
            try {
                movement.readerIndex(editor.cursor.pos); Types.LOW_PRECISION_VECTOR.read(movement);
                editor.cursor.pos = movement.readerIndex();
            } finally { movement.release(); }
            editor.cursor.skip(3);
            if (EntityTypes1_21_11.getTypeFromId(type).isOrHasParent(EntityTypes1_21_11.PROJECTILE)) editor.varOwnerId();
        } else if (id == ClientboundPackets1_21_11.DAMAGE_EVENT.getId()) {
            editor.cursor.vi(); // victim was rewritten by v5
            editor.cursor.vi(); // fixed registry holder for DamageType
            editor.varId(true); editor.varId(true);
        } else return replay;
        return editor.finish();
    }

    static int copySnapshot(ViaVersionPacketConverter converter, Path path, OutputStream out,
                            boolean required, boolean localHotbar) throws Exception {
        int[] count = {0};
        forEachPacket(path, required, raw -> {
            if (varInt(raw) == JOIN) throw new IOException("Unexpected Join Game in " + path.getFileName());
            for (byte[] converted : convert(converter, raw, State.PLAY)) {
                byte[] packet = modernReplayPacket(converted, localHotbar);
                if (packet != null) {
                    byte[] profile = playerInfoBeforeSpawn(packet, "snapshot");
                    if (profile != null) frame(out, count[0]++, profile);
                    frame(out, count[0]++, packet);
                }
            }
        });
        return count[0];
    }

    static int copyRuntime(ViaVersionPacketConverter converter, Path path, OutputStream out,
                           OutputStream timeline, boolean localHotbar) throws Exception {
        Path configurationsPath = path.resolveSibling("runtime-configurations.bin");
        if (!Files.exists(path)) {
            if (Files.exists(configurationsPath) && Files.size(configurationsPath) != 0)
                throw new IOException("Runtime configurations have no corresponding runtime packet stream");
            return 0;
        }
        int count = 0; long previousTick = -1; boolean configuring = false;
        try (DataInputStream in = new DataInputStream(new BufferedInputStream(Files.newInputStream(path)));
             DataInputStream configurations = new DataInputStream(Files.exists(configurationsPath)
                 ? new BufferedInputStream(Files.newInputStream(configurationsPath)) : new ByteArrayInputStream(new byte[0]))) {
            ConfigurationEvent nextConfiguration = readConfigurationEvent(configurations, -1, -1);
            for (long packetIndex = 0; in.available() > 0; packetIndex++) {
                long tick = in.readLong();
                if (tick < 0 || tick < previousTick) throw new IOException("Non-monotonic runtime tick: " + tick);
                previousTick = tick;
                byte[] raw = readPacket(in, in.readInt(), path.getFileName().toString());
                int id = varInt(raw);
                try {
                    if (nextConfiguration != null && nextConfiguration.beforePacketIndex() <= packetIndex) {
                        if (nextConfiguration.beforePacketIndex() != packetIndex || nextConfiguration.tick() != tick || id != JOIN)
                            throw new IOException("Runtime configuration is not bound to its exact Join Game packet/tick");
                        // Proxies can replace every registry and entity id.
                        // Rebuild Via's connection just as the legacy adapter
                        // does for a fresh server, keeping our replay-local id.
                        converter = newModernConverter();
                        writeRuntimeConfiguration(converter, nextConfiguration, timeline);
                        Cursor joinCursor = new Cursor(raw); joinCursor.vi();
                        sourcePlayerId = joinCursor.intValue();
                        if (sourcePlayerId < 0) throw new IOException("Invalid transferred source player entity id");
                        modernJoin(raw, false);
                        presentProfiles.clear(); lastProfiles.clear();
                        int joins = 0;
                        for (byte[] converted : convert(converter, raw, State.PLAY)) {
                            boolean join = varInt(converted) == TARGET_JOIN;
                            byte[] packet = join ? modernJoin(converted, true) : modernReplayPacket(converted, localHotbar);
                            if (packet == null) continue;
                            frameLong(out, tick, packet); timelineFrame(timeline, tick, 0, packet); count++;
                            if (join) {
                                joins++;
                                byte[] localInfo = localPlayerInfo(); observePlayerInfo(localInfo);
                                frameLong(out, tick, localInfo); timelineFrame(timeline, tick, 0, localInfo); count++;
                                timelineFrame(timeline, tick, 2, createLocalMarker());
                            }
                        }
                        if (joins != 1) throw new IOException("Transferred configuration did not produce exactly one Join Game");
                        count("worldTransitions"); configuring = false;
                        nextConfiguration = readConfigurationEvent(configurations, nextConfiguration.beforePacketIndex(), nextConfiguration.tick());
                        continue;
                    }
                    if (id == JOIN) throw new IOException("Runtime Join Game is missing its complete configuration snapshot");
                    if (id == ClientboundPackets1_21_9.START_CONFIGURATION.getId()) {
                        if (configuring) throw new IOException("Nested server reconfiguration before a completed Join Game");
                        configuring = true; count("serverConfigurationStarts");
                        // Flashback receives complete configuration actions at
                        // the matching LOGIN, not a live connection handshake.
                        continue;
                    }
                    if (configuring) throw new IOException("Play packet recorded during an unfinished server configuration");
                    for (byte[] converted : convert(converter, raw, State.PLAY)) {
                        boolean respawn = varInt(converted) == ClientboundPackets1_21_11.RESPAWN.getId();
                        byte[] packet = respawn ? modernRespawn(converted) : modernReplayPacket(converted, localHotbar);
                        if (packet == null) continue;
                        byte[] profile = playerInfoBeforeSpawn(packet, "runtime");
                        if (profile != null) { frameLong(out, tick, profile); timelineFrame(timeline, tick, 0, profile); count++; }
                        frameLong(out, tick, packet); timelineFrame(timeline, tick, 0, packet); count++;
                        if (respawn) {
                            // ReplayGamePacketHandler retains/recreates its
                            // local entity on Respawn. CreateLocalPlayer then
                            // moves that entity rather than creating a duplicate.
                            timelineFrame(timeline, tick, 2, createLocalMarker());
                            count("worldRespawns");
                        }
                    }
                } catch (Exception error) {
                    throw new IOException("Modern conversion failed at tick " + tick + ", source packet " + id, error);
                }
            }
            if (nextConfiguration != null) throw new IOException("Runtime configuration refers to a missing Join Game packet");
        }
        // Stopping or disconnecting during transfer leaves the preceding
        // complete world playable. No destination is invented or emitted.
        if (configuring) count("incompleteWorldTransitions");
        return count;
    }

    /** captureFormat 5, no header: BE int64 tick, int64 beforePacketIndex,
     * int32 packetCount, then int32 size + raw configuration packet. The
     * ordinal counts every successfully written source runtime frame. */
    static ConfigurationEvent readConfigurationEvent(DataInputStream in, long previousIndex, long previousTick) throws IOException {
        if (in.available() == 0) return null;
        long tick = in.readLong(), index = in.readLong(); int packets = in.readInt();
        if (tick < 0 || tick < previousTick || index < 0 || index <= previousIndex || packets < 1 || packets > 65536)
            throw new IOException("Invalid runtime configuration event header/order");
        List<byte[]> result = new ArrayList<>(packets); long bytes = 0;
        for (int i = 0; i < packets; i++) {
            int size = in.readInt(); bytes += size;
            if (size <= 0 || bytes > MAX_PACKET) throw new IOException("Runtime configuration event exceeds size limit");
            result.add(readPacket(in, size, "runtime-configurations.bin"));
        }
        return new ConfigurationEvent(tick, index, result);
    }

    static ViaVersionPacketConverter newModernConverter() throws Exception {
        ViaVersionPacketConverter converter = ViaVersionPacketConverter.createForProtocolVersion(SOURCE, TARGET);
        convert(converter, modernLogin(), State.LOGIN); converter.loginAcknowledged(); return converter;
    }

    static void writeRuntimeConfiguration(ViaVersionPacketConverter converter, ConfigurationEvent event, OutputStream timeline) throws Exception {
        int registries = 0, finishes = 0;
        for (byte[] raw : event.packets()) {
            if (finishes != 0) throw new IOException("Runtime configuration has packets after Finish Configuration");
            int id = varInt(raw);
            if (id == ClientboundConfigurationPackets1_21_9.REGISTRY_DATA.getId()) registries++;
            if (id == ClientboundConfigurationPackets1_21_9.FINISH_CONFIGURATION.getId()) finishes++;
            if (id == ClientboundConfigurationPackets1_21_9.UPDATE_TAGS.getId()) raw = vanillaRegistryTags(raw, "configuration");
            for (byte[] packet : convert(converter, raw, State.CONFIGURATION)) {
                if (isUnsupportedConfiguration(packet)) continue;
                timelineFrame(timeline, event.tick(), 1, packet); count("dynamicConfigurationPackets");
            }
        }
        if (registries == 0 || finishes != 1) throw new IOException("Runtime configuration snapshot is incomplete");
        converter.finishConfiguration();
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 1) throw new IllegalArgumentException("usage: ModernFlashbackProtocolAdapter <sessionDir>");
        Path dir = Paths.get(args[0]); diagnostics.clear();
        // A failed retry must never leave a success marker from a previous run.
        Files.deleteIfExists(dir.resolve("converted.properties"));
        loadRecordedProfileNames(dir.resolve("recorded-skins.json"));
        Properties identity = readProperties(dir.resolve("replay-player.properties"), true);
        Properties capture = readProperties(dir.resolve("capture.properties"), true);
        int protocol = Integer.parseInt(capture.getProperty("sourceProtocol", identity.getProperty("sourceProtocol", "-1")));
        if (protocol != SOURCE) throw new IOException("Modern adapter requires source protocol 773, got " + protocol);
        sourcePlayerId = Integer.parseInt(identity.getProperty("sourcePlayerId"));
        if (sourcePlayerId < 0) throw new IOException("Missing source player entity id");
        replayPlayerId = ~sourcePlayerId;
        playerUuid = UUID.fromString(identity.getProperty("playerUuid"));
        playerName = new String(Base64.getDecoder().decode(identity.getProperty("playerNameBase64")), StandardCharsets.UTF_8);
        if (playerName.isBlank() || playerName.length() > 16) throw new IOException("Invalid player profile name");
        playerGametype = Integer.parseInt(identity.getProperty("gametype", "0"));
        boolean localHotbar = "true".equals(capture.getProperty("localHotbarSlots"));
        if (!ViaVersionPacketConverter.isProtocolVersionSupported(SOURCE, TARGET)) throw new IOException("Bundled converter does not support 773 -> 774");
        ViaVersionPacketConverter converter = newModernConverter();
        int[] configs = {0}, registryPackets = {0}, configurationFinish = {0};
        try (OutputStream out = Files.newOutputStream(dir.resolve("converted-config.bin"))) {
            forEachPacket(dir.resolve("snapshot-config.bin"), true, raw -> {
                if (configurationFinish[0] != 0) throw new IOException("Configuration snapshot has packets after Finish Configuration");
                int id = varInt(raw);
                if (id == ClientboundConfigurationPackets1_21_9.REGISTRY_DATA.getId()) registryPackets[0]++;
                if (id == ClientboundConfigurationPackets1_21_9.FINISH_CONFIGURATION.getId()) configurationFinish[0]++;
                if (id == ClientboundConfigurationPackets1_21_9.UPDATE_TAGS.getId()) raw = vanillaRegistryTags(raw, "configuration");
                for (byte[] packet : convert(converter, raw, State.CONFIGURATION)) {
                    if (!isUnsupportedConfiguration(packet)) frame(out, configs[0]++, packet);
                }
            });
        }
        if (registryPackets[0] == 0) throw new IOException("Modern configuration snapshot has no registry data");
        if (configurationFinish[0] != 1) throw new IOException("Modern configuration snapshot did not finish");
        converter.finishConfiguration();
        byte[] join = modernJoin(Files.readAllBytes(dir.resolve("snapshot-packets.bin")), false);
        int games = 0, joins = 0;
        try (OutputStream out = Files.newOutputStream(dir.resolve("converted-game-snapshot.bin"))) {
            for (byte[] packet : convert(converter, join, State.PLAY)) {
                if (varInt(packet) == TARGET_JOIN) { packet = modernJoin(packet, true); joins++; }
                else packet = modernReplayPacket(packet, localHotbar);
                if (packet != null) frame(out, games++, packet);
            }
            if (joins != 1) throw new IOException("Expected one converted Join Game, got " + joins);
            byte[] localInfo = localPlayerInfo(); observePlayerInfo(localInfo); frame(out, games++, localInfo);
        }
        int entities, world, chunks, dynamic;
        try (OutputStream out = Files.newOutputStream(dir.resolve("converted-entities.bin"))) {
            entities = copySnapshot(converter, dir.resolve("snapshot-entities.bin"), out, false, localHotbar);
        }
        try (OutputStream out = Files.newOutputStream(dir.resolve("converted-world.bin"))) {
            world = copySnapshot(converter, dir.resolve("snapshot-world.bin"), out, false, localHotbar);
        }
        try (OutputStream out = Files.newOutputStream(dir.resolve("converted-chunks.bin"))) {
            chunks = copySnapshot(converter, dir.resolve("snapshot-chunks.bin"), out, true, localHotbar);
        }
        try (OutputStream out = Files.newOutputStream(dir.resolve("converted-packets.bin"));
             OutputStream timeline = Files.newOutputStream(dir.resolve("converted-timeline.bin"))) {
            dynamic = copyRuntime(converter, dir.resolve("runtime-packets.bin"), out, timeline, localHotbar);
        }
        Properties result = new Properties();
        result.setProperty("sourceProtocol", "773"); result.setProperty("targetProtocol", "774");
        result.setProperty("configurationPackets", Integer.toString(configs[0]));
        result.setProperty("gameSnapshotPackets", Integer.toString(games));
        result.setProperty("entityPackets", Integer.toString(entities));
        result.setProperty("worldSnapshotPackets", Integer.toString(world));
        result.setProperty("chunkPackets", Integer.toString(chunks));
        result.setProperty("dynamicPackets", Integer.toString(dynamic));
        result.setProperty("dynamicPacketErrors", "0"); result.setProperty("targetGameIds", "48,38");
        result.setProperty("adapterVersion", "6"); result.setProperty("timelineFormat", "1");
        result.setProperty("customRegistryTagGroupsDropped", Integer.toString(diagnostics.getOrDefault("configuration.customRegistryTagGroupsDropped", 0)
            + diagnostics.getOrDefault("play.customRegistryTagGroupsDropped", 0)));
        result.setProperty("emptyRegistryTagGroupsDropped", Integer.toString(diagnostics.getOrDefault("configuration.emptyRegistryTagGroupsDropped", 0)
            + diagnostics.getOrDefault("play.emptyRegistryTagGroupsDropped", 0)));
        result.setProperty("registryTagCompatibility", "nonempty-minecraft-registry-groups;mod-tags-on-minecraft-registries-preserved");
        for (String key : List.of("playerSpawns", "playerProfilesRecoveredFromManifest", "playerProfilesRestored", "playerProfilesMissing"))
            result.setProperty(key, Integer.toString(diagnostics.getOrDefault("snapshot." + key, 0) + diagnostics.getOrDefault("runtime." + key, 0)));
        result.setProperty("recordedProfileManifestInvalid", Integer.toString(diagnostics.getOrDefault("recordedProfileManifestInvalid", 0)));
        for (String key : List.of("worldTransitions", "serverConfigurationStarts", "incompleteWorldTransitions", "dynamicConfigurationPackets"))
            result.setProperty(key, Integer.toString(diagnostics.getOrDefault(key, 0)));
        result.setProperty("flashbackVersion", "0.39.10-for-MC1.21.11");
        result.setProperty("localHotbarSlots", Boolean.toString(localHotbar));
        for (var entry : diagnostics.entrySet()) result.setProperty("diagnostic." + entry.getKey(), entry.getValue().toString());
        try (OutputStream out = Files.newOutputStream(dir.resolve("converted.properties"))) { result.store(out, "Modern Flashback protocol adapter"); }
        System.out.println("source=773 target=774 configuration=" + configs[0] + " gameSnapshot=" + games + " entities=" + entities
            + " chunks=" + chunks + " dynamicPackets=" + dynamic + " dynamicPacketErrors=0");
        System.out.println("packetDiagnostics=" + diagnostics);
    }
}
