package local.flashback.modern.impl;

import com.google.gson.Gson;
import com.mojang.datafixers.util.Pair;
import io.netty.buffer.ByteBuf;
import io.netty.buffer.Unpooled;
import java.io.*;
import java.lang.reflect.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import java.util.function.*;
import net.minecraft.SharedConstants;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.*;
import net.minecraft.core.*;
import net.minecraft.nbt.NbtOps;
import net.minecraft.network.*;
import net.minecraft.network.codec.StreamCodec;
import net.minecraft.network.protocol.*;
import net.minecraft.network.protocol.common.ClientboundUpdateTagsPacket;
import net.minecraft.network.protocol.configuration.*;
import net.minecraft.network.protocol.game.*;
import net.minecraft.network.syncher.SynchedEntityData;
import net.minecraft.resources.*;
import net.minecraft.tags.TagNetworkSerialization;
import net.minecraft.world.entity.*;
import net.minecraft.world.flag.FeatureFlags;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.*;
import net.minecraft.world.level.chunk.LevelChunk;
import net.minecraft.world.scores.*;

/** A separate 1.21.10 implementation. All world reads and packet callbacks run on the client thread. */
@SuppressWarnings({"rawtypes", "unchecked"})
public final class Recorder implements Consumer<Object>, Runnable {
    private static final Gson JSON = new Gson();
    private final Path stateDir;
    private final Minecraft mc = Minecraft.getInstance();
    private final Map<String,String> status = new LinkedHashMap<>();
    private final ConcurrentLinkedQueue<Encoded> packets = new ConcurrentLinkedQueue<>();
    private final AtomicLong queuedBytes = new AtomicLong(), errors = new AtomicLong(), drops = new AtomicLong();
    private final Map<String,byte[]> lastLocal = new HashMap<>();
    private volatile boolean running = true, recording;
    private volatile long origin;
    private volatile StreamCodec playCodec;
    private volatile String lastError = "";
    private Path recordingDir, replay;
    private BufferedWriter timeline;
    private DataOutputStream packetFile;
    private DataOutputStream configurationFile;
    private long commandModified, count, samples, lastStatus, lastLocalTick = -1;
    private long skipped, localAnimations;
    private int sampleRate = 20, oldSwingTime;
    private Object recordedLevel;
    private Object recordedConnection;
    private volatile boolean transitioning;
    private boolean pendingLogin;
    private volatile long transitionStarted;
    private volatile String transitionReason="";
    private long configurationTransitions;
    private int sourcePlayerId;
    private long loginTick=-1;
    private int sampleTimeouts;
    private boolean captureSealed=true;
    private Thread worker;
    private volatile String captureError="";
    private long samplingTick=-1;
    private List<Map<String,Object>> remoteState=List.of();
    private boolean hasEffects;
    private long localSounds,localEvents,localDestruction,localPredictions,nextSkinTick;
    private net.minecraft.core.BlockPos lastDestroyPos;
    private int lastDestroyStage=-1;
    private RecordedSkinCapture skins;
    private RecordedSkinCapture.Writer skinWriter;
    private volatile RecordedSkinCapture.Batch pendingSkins;
    private volatile String skinError="";
    private long skinErrors;
    private Runnable exitAction=()->{};
    private java.lang.instrument.Instrumentation instrumentation;
    private volatile ReceiverHook receiverHook;
    private record Encoded(long tick, long time, byte[] bytes,List<byte[]> configuration) {
        Encoded(long tick,long time,byte[] bytes){this(tick,time,bytes,List.of());}
        long size(){return bytes.length+configuration.stream().mapToLong(value->value.length).sum();}
    }

    public Recorder(Path stateDir) { this.stateDir = stateDir; }
    public boolean isRecording(){return recording;}
    public void effectsAvailable(boolean available){hasEffects=available;}
    public void onExit(Runnable action){exitAction=()->{uninstallReceiverHook();action.run();};}
    public void shutdown() throws InterruptedException {
        if(recording)throw new IllegalStateException("Cannot replace an active recorder");
        running=false;
        if(worker!=null){worker.join(15_000);if(worker.isAlive())throw new IllegalStateException("Previous recorder did not stop");}
        else uninstallReceiverHook();
    }

    public void start(int handlers) throws IOException {
        openCapturePackages();
        installReceiverHook();
        Path command = stateDir.resolve("command.txt");
        commandModified = Files.exists(command) ? Files.getLastModifiedTime(command).toMillis() : 0;
        status.put("attached", "true"); status.put("agentVersion", "17");
        status.put("pid",""+ProcessHandle.current().pid());
        status.put("minecraftVersion", "1.21.10"); status.put("sourceProtocol", "773");
        status.put("packetHook", "installed-receiver"); status.put("packetHandlers", ""+receiverHook.methods);
        status.put("recording", "false"); status.put("mode", "timeline"); status.put("state", "ready");
        status.put("localEffectsHook",hasEffects?"installed":"error: local effect hook sites did not match");
        status.put("skinCapture","available-cache-only");
        publish();
        worker = new Thread(this, "Flashback-Modern-Recorder"); worker.setDaemon(true); worker.start();
    }

    @Override public void accept(Object value) {
        if (!recording || !mc.isSameThread())return;
        ClientPacketListener listener;
        Packet<?> packet;
        if(value instanceof Object[] event && event.length==2 && event[0] instanceof ClientPacketListener receiver && event[1] instanceof Packet<?> received) {
            listener=receiver;packet=received;
        } else {
            if(value instanceof Object[] event && !transitioning)captureEffect(event);
            return;
        }
        // Scheduled callbacks from a retired listener must not enter the new world's stream.
        if(!(packet instanceof ClientboundLoginPacket) && listener!=recordedConnection)return;
        if(packet instanceof ClientboundPlayerInfoUpdatePacket && skins!=null)try{skins.observePlayerInfo(packet);}catch(Exception error){skinErrors++;skinError=describe(error);}
        String name = packet.getClass().getSimpleName();
        if(name.contains("CustomPayload")){skipped++;return;}
        if(packet instanceof ClientboundStartConfigurationPacket) {
            queue(packet);beginTransition("server-configuration");pendingLogin=false;return;
        }
        if(packet instanceof ClientboundLoginPacket login) {
            try {
                playCodec=GameProtocols.CLIENTBOUND_TEMPLATE.bind(RegistryFriendlyByteBuf.decorator(listener.registryAccess())).codec();
                List<byte[]> configuration=configurationSnapshot(listener);
                byte[] bytes=encode(playCodec,packet);
                long tick=tick();
                Encoded queued=new Encoded(tick,elapsedMillis(),bytes,configuration);
                if(queuedBytes.addAndGet(queued.size())>64L*1024*1024) {
                    queuedBytes.addAndGet(-queued.size());throw new IllegalStateException("Transfer configuration exceeds recorder queue capacity");
                }
                packets.add(queued);
                beginTransition("loading-new-world");pendingLogin=true;recordedConnection=listener;
                sourcePlayerId=login.playerId();loginTick=tick;configurationTransitions++;
            }catch(Throwable error){captureError="Transfer snapshot: "+describe(error);}
            return;
        }
        if(transitioning&&!pendingLogin)return;
        // Bundles invoke their children on the same client listener. Control packets
        // and screen-only state have no replay world representation.
        if (name.equals("ClientboundBundlePacket")
                || name.contains("KeepAlive") || name.contains("Ping") || name.contains("Pong")
                || name.startsWith("ClientboundContainer") || name.equals("ClientboundSetHealthPacket")
                || name.equals("ClientboundSetExperiencePacket") || name.equals("ClientboundSetHeldSlotPacket")) { return; }
        queue(packet);
    }

    private void queue(Packet<?> packet) {
        try { queueBytes(encode(playCodec, packet)); }
        catch (Throwable error) { packetError(packet.getClass().getSimpleName(), error); }
    }
    private void queueBytes(byte[] bytes) {
        if (!recording) return;
        if (queuedBytes.addAndGet(bytes.length) > 64L*1024*1024) {
            queuedBytes.addAndGet(-bytes.length); drops.incrementAndGet(); return;
        }
        packets.add(new Encoded(samplingTick>=0?samplingTick:tick(), elapsedMillis(), bytes));
    }
    private long tick() { return Math.max(0, (System.nanoTime()-origin)/50_000_000L); }
    private long elapsedMillis() { return Math.max(0, (System.nanoTime()-origin)/1_000_000L); }
    private void beginTransition(String reason) {
        if(!transitioning)transitionStarted=System.currentTimeMillis();
        transitioning=true;transitionReason=reason;
    }
    private void packetError(String context, Throwable error) { errors.incrementAndGet(); lastError = context+": "+describe(error); }

    @Override public void run() {
        try {
            while (running) {
                long started = System.nanoTime();
                try {
                    readCommand();
                    if (recording) {
                        if(!captureError.isEmpty())throw new IllegalStateException(captureError);
                        Map<String,Object> sample;
                        try{sample=onClient(this::sample,5_000);sampleTimeouts=0;}
                        catch(TimeoutException busy){
                            if(++sampleTimeouts>=12)throw new IllegalStateException("Minecraft client thread did not respond for 60 seconds",busy);
                            sample=null;
                        }
                        if (sample != null) { timeline.write(JSON.toJson(sample)); timeline.newLine(); samples++; }
                        drain();
                        flushSkins();
                    }
                } catch (Throwable error) {
                    captureError=describe(error);
                    status.put("error", describe(error));
                    status.put("sampleError",captureError);status.put("stopWarning",captureError);
                    status.put("state", recording ? "record-error" : "error");
                    if (recording) try {stopRecording();}catch(Exception stopFailure){recording=false;closeStreams();status.put("recording","false");status.put("stopWarning",describe(stopFailure));}
                    if(!captureSealed){closeStreams();sealCapture();}
                    log("loop: "+describe(error));
                }
                if (System.currentTimeMillis()-lastStatus > 500) { counters(); publish(); lastStatus=System.currentTimeMillis(); }
                long delay=(recording ? 1_000_000_000L/sampleRate : 50_000_000L)-(System.nanoTime()-started);
                if (delay>0) TimeUnit.NANOSECONDS.sleep(delay);
            }
        } catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
        finally {
            try { stopRecording(); } catch (Throwable error) { captureError=describe(error);status.put("stopWarning",captureError);log(captureError); }
            recording=false;
            try{drain();}catch(Throwable error){log("final drain: "+describe(error));}
            closeStreams();status.put("recording","false");
            if(recordingDir!=null && !captureError.isEmpty())try{Files.writeString(recordingDir.resolve("capture.properties"),"captureError="+captureError.replace('\n',' ')+"\nstopWarning="+captureError.replace('\n',' ')+"\n",StandardCharsets.UTF_8,StandardOpenOption.APPEND);}catch(IOException ignored){}
            sealCapture();
            status.put("state", "stopped"); publish();
            exitAction.run();
        }
    }

    private void readCommand() throws Exception {
        Path file = stateDir.resolve("command.txt");
        if (!Files.isRegularFile(file)) return;
        long modified=Files.getLastModifiedTime(file).toMillis();
        if (modified==commandModified) return;
        commandModified=modified;
        String command=Files.readString(file, StandardCharsets.UTF_8).trim();
        if (command.equalsIgnoreCase("start") || command.startsWith("start:")) {
            if (recording) return;
            int rate=command.equalsIgnoreCase("start") ? 20 : Integer.parseInt(command.substring(6).trim());
            if (rate<1 || rate>240) throw new IllegalArgumentException("Sampling rate must be 1..240");
            sampleRate=rate; startRecording();
        } else if (command.equalsIgnoreCase("stop")) stopRecording();
        else if (command.equalsIgnoreCase("quit")) running=false;
        else if (command.startsWith("mark:") && recording) {
            timeline.write(JSON.toJson(Map.of("type","mark","t",elapsedMillis(),"tick",tick(),"name",command.substring(5)))); timeline.newLine();
        }
    }

    private void startRecording() throws Exception {
        status.put("error", ""); status.put("packetError", ""); status.put("replay", "");
        captureError="";status.put("sampleError","");status.put("stopWarning",hasEffects?"":"Local effect hook unavailable");
        status.put("state", "capturing-snapshot"); publish();
        String stamp=DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss-SSS").format(LocalDateTime.now());
        recordingDir=stateDir.resolve("recording-"+stamp+"-modern"); Files.createDirectory(recordingDir);
        captureSealed=false;
        replay=recordingDir.resolve("flashback-lite-"+stamp+".fbrl");
        count=samples=skipped=localAnimations=0; errors.set(0);drops.set(0);queuedBytes.set(0);packets.clear();lastError="";
        localSounds=localEvents=localDestruction=localPredictions=nextSkinTick=skinErrors=0;skinError="";lastDestroyPos=null;lastDestroyStage=-1;pendingSkins=null;
        transitioning=false;pendingLogin=false;transitionStarted=0;transitionReason="";configurationTransitions=0;sampleTimeouts=0;loginTick=-1;
        skinWriter=new RecordedSkinCapture.Writer(recordingDir);
        timeline=Files.newBufferedWriter(replay,StandardCharsets.UTF_8,StandardOpenOption.CREATE_NEW);
        packetFile=new DataOutputStream(new BufferedOutputStream(Files.newOutputStream(recordingDir.resolve("runtime-packets.bin"),StandardOpenOption.CREATE_NEW)));
        configurationFile=new DataOutputStream(new BufferedOutputStream(Files.newOutputStream(recordingDir.resolve("runtime-configurations.bin"),StandardOpenOption.CREATE_NEW)));
        Files.writeString(recordingDir.resolve("capture.properties"),
                "captureFormat=5\nagentVersion=17\nminecraftVersion=1.21.10\nsourceProtocol=773\nsourceProtocolVersion=773\n"+
                "runtimeConfigurationFormat=1\nconfigurationPacketIndex=zero-based-runtime-frame\n"+
                "captureClosed=false\n"+
                "localHotbarSlots=true\nlocalEquipment=true\nlocalMetadata=true\nlocalEntityIds=source\ntickClock=monotonic-20hz\n"+
                "customPayloads=not-captured-cross-version\nlocalEffects="+(hasEffects?"installed":"unavailable")+"\nskinCaptureVersion=2\nsamplingRate="+sampleRate+"\n",StandardCharsets.UTF_8);
        AtomicBoolean active=new AtomicBoolean(true);
        AtomicReference<Map<String,Object>> initialSample=new AtomicReference<>();
        try {
            Map<String,Object> metadata=onClient(() -> {
                if (!active.get()) throw new IllegalStateException("Snapshot cancelled");
                if (mc.player==null || mc.level==null || mc.getConnection()==null) throw new IllegalStateException("Enter a world before recording");
                if (!SharedConstants.getCurrentVersion().name().equals("1.21.10") || SharedConstants.getCurrentVersion().protocolVersion()!=773)
                    throw new IllegalStateException("This agent requires Minecraft 1.21.10 protocol 773");
                playCodec=GameProtocols.CLIENTBOUND_TEMPLATE.bind(RegistryFriendlyByteBuf.decorator(mc.getConnection().registryAccess())).codec();
                skins=new RecordedSkinCapture(mc.getClass().getClassLoader());
                Map<String,Object> meta=captureSnapshot();
                captureSkins();
                synchronized(active) {
                    if (!active.get()) throw new IllegalStateException("Snapshot start cancelled");
                    recordedLevel=mc.level;recordedConnection=mc.getConnection();sourcePlayerId=mc.player.getId();lastLocal.clear(); lastLocalTick=-1; oldSwingTime=0;
                    origin=System.nanoTime(); recording=true;
                    // Establish the initial local state before any queued transfer packet can run.
                    // Otherwise an immediate hub switch could precede the first worker sample.
                    initialSample.set(sample());
                }
                return meta;
            },60_000);
            timeline.write(JSON.toJson(metadata)); timeline.newLine(); timeline.flush();
            if(initialSample.get()!=null){timeline.write(JSON.toJson(initialSample.get()));timeline.newLine();timeline.flush();samples++;}
            flushSkins();
            status.put("state","ready");status.put("recording","true");status.put("replay",replay.toString());
            status.put("samplingRate",""+sampleRate); counters();publish();
        } catch (Throwable error) {
            synchronized(active) { active.set(false); recording=false; }
            captureError=describe(error);
            closeStreams();status.put("recording","false");status.put("state","record-error");
            status.put("error",describe(error));status.put("stopWarning",captureError);sealCapture();publish();log("start: "+describe(error));
        }
    }

    private void stopRecording() throws Exception {
        if (!recording) return;
        // Queue after every earlier client callback; do not close a stream while its capture is running.
        onClient(() -> { captureSkins();recording=false; return null; },10_000);
        if(transitioning&&status.getOrDefault("stopWarning","").isEmpty())status.put("stopWarning","Recording ended while waiting for the next world; completed worlds remain exportable");
        drain();flushSkins();closeStreams();
        Files.writeString(recordingDir.resolve("capture.properties"),"packetErrors="+errors.get()+"\npacketDrops="+drops.get()+
                "\npacketSkippedCustomPayload="+skipped+"\nrecordedSkins="+skinWriter.count()+"\nskinCaptureErrors="+(skinErrors+skinWriter.errors())+"\nconfigurationTransitions="+configurationTransitions+"\nendedDuringTransition="+transitioning+"\ncaptureError="+captureError.replace('\n',' ')+"\nstopWarning="+status.getOrDefault("stopWarning","")+"\n",StandardCharsets.UTF_8,StandardOpenOption.APPEND);
        sealCapture();
        status.put("recording","false");status.put("state",captureError.isEmpty()?"ready":"record-error");counters();publish();
    }
    private void closeStreams() {
        if (packetFile!=null) try { packetFile.close(); } catch(IOException error) {closeFailure(error);} finally {packetFile=null;}
        if (configurationFile!=null) try { configurationFile.close(); } catch(IOException error) {closeFailure(error);} finally {configurationFile=null;}
        if (timeline!=null) try { timeline.close(); } catch(IOException error) {closeFailure(error);} finally {timeline=null;}
    }
    private void closeFailure(Throwable error) {
        String message="Capture file close: "+describe(error);
        captureError=captureError.isEmpty()?message:captureError+"; "+message;
        status.put("stopWarning",captureError);status.put("error",captureError);log(message);
    }
    private void sealCapture() {
        if(captureSealed||recordingDir==null)return;
        try {
            Files.writeString(recordingDir.resolve("capture.properties"),"captureError="+captureError.replace('\n',' ')+"\nstopWarning="+status.getOrDefault("stopWarning","").replace('\n',' ')+"\ncaptureClosed=true\n",StandardCharsets.UTF_8,StandardOpenOption.CREATE,StandardOpenOption.APPEND);
            captureSealed=true;
        }catch(IOException error){closeFailure(error);}
    }
    private void drain() throws IOException {
        if (packetFile==null || timeline==null) return;
        for (Encoded packet;(packet=packets.poll())!=null;) {
            queuedBytes.addAndGet(-packet.size());
            writeRuntimeFrame(packetFile,configurationFile,packet.tick,count,packet.bytes,packet.configuration);
            timeline.write(JSON.toJson(Map.of("type","packet","t",packet.time,"tick",packet.tick,"bytes",packet.bytes.length)));timeline.newLine();count++;
        }
        packetFile.flush(); timeline.flush();
        configurationFile.flush();
    }
    static void writeRuntimeFrame(DataOutputStream runtime,DataOutputStream configuration,long tick,long index,byte[] packet,List<byte[]> snapshot)throws IOException {
        if(!snapshot.isEmpty()) {
            configuration.writeLong(tick);configuration.writeLong(index);configuration.writeInt(snapshot.size());
            for(byte[] bytes:snapshot){configuration.writeInt(bytes.length);configuration.write(bytes);}
        }
        runtime.writeLong(tick);runtime.writeInt(packet.length);runtime.write(packet);
    }
    private void counters() {
        status.put("samples",""+samples);status.put("packets",""+count);status.put("packetErrors",""+errors.get());
        status.put("packetDrops",""+drops.get());status.put("packetError",lastError);status.put("packetQueueBytes",""+queuedBytes.get());
        status.put("packetSkippedCustomPayload",""+skipped);status.put("localAnimationPackets",""+localAnimations);
        status.put("localSoundPackets",""+localSounds);status.put("localLevelEventPackets",""+localEvents);status.put("localDestructionPackets",""+localDestruction);status.put("localPredictionPackets",""+localPredictions);
        status.put("recordedSkins",""+(skinWriter==null?0:skinWriter.count()));status.put("skinCaptureErrors",""+(skinErrors+(skinWriter==null?0:skinWriter.errors())));status.put("skinError",skinError);
        status.put("configurationTransitions",""+configurationTransitions);status.put("worldTransition",transitioning?transitionReason:"");
        status.put("worldTransitionMillis",""+(transitioning?Math.max(0,System.currentTimeMillis()-transitionStarted):0));
    }
    private synchronized void publish() {
        try {
            StringBuilder text=new StringBuilder();status.forEach((key,value)->text.append(key).append('=').append(value.replace('\n',' ').replace('\r',' ')).append('\n'));
            Path temporary=stateDir.resolve("modern-status.tmp");Files.writeString(temporary,text,StandardCharsets.UTF_8);
            try {Files.move(temporary,stateDir.resolve("status.properties"),StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING);}
            catch(AtomicMoveNotSupportedException ignored){Files.move(temporary,stateDir.resolve("status.properties"),StandardCopyOption.REPLACE_EXISTING);}
        } catch(IOException ignored) {}
    }
    private void log(String text) {try {Files.writeString(stateDir.resolve("agent-diagnostics.log"),Instant.now()+" modern "+text+"\n",StandardCharsets.UTF_8,StandardOpenOption.CREATE,StandardOpenOption.APPEND);}catch(IOException ignored){}}
    private static String describe(Throwable value) {
        while ((value instanceof ExecutionException || value instanceof InvocationTargetException) && value.getCause()!=null) value=value.getCause();
        return value.getClass().getSimpleName()+": "+value.getMessage();
    }
    private <T> T onClient(Callable<T> operation,long timeout) throws Exception {
        if(mc.isSameThread()) return operation.call();
        CompletableFuture<T> result=new CompletableFuture<>();
        mc.execute(()->{if(result.isCancelled())return;try{result.complete(operation.call());}catch(Throwable e){result.completeExceptionally(e);}});
        try{return result.get(timeout,TimeUnit.MILLISECONDS);}catch(TimeoutException timeoutError){result.cancel(false);throw timeoutError;}
    }
    private static byte[] encode(StreamCodec codec, Object packet) {
        ByteBuf buffer=Unpooled.buffer();
        try{codec.encode(buffer,packet);byte[] result=new byte[buffer.readableBytes()];buffer.readBytes(result);return result;}
        finally{buffer.release();}
    }
    private static Object field(Object owner,String name) throws ReflectiveOperationException {
        for(Class<?> type=owner.getClass();type!=null;type=type.getSuperclass())try{Field f=type.getDeclaredField(name);f.setAccessible(true);return f.get(owner);}catch(NoSuchFieldException ignored){}
        throw new NoSuchFieldException(owner.getClass().getName()+"."+name);
    }
    private static void setField(Object owner,String name,Object value) throws ReflectiveOperationException {
        Field f=owner.getClass().getDeclaredField(name);f.setAccessible(true);f.set(owner,value);
    }
    private void openCapturePackages() throws IOException {
        // The entry shell can already be loaded in this JVM. Open new implementation-only
        // reflection sites here so replacing its implementation JAR does not need a new shell.
        try {
            Class<?> shell=Class.forName("local.flashback.modern.AgentV2",false,ClassLoader.getSystemClassLoader());
            Field field=shell.getDeclaredField("installedInstrumentation");field.setAccessible(true);
            instrumentation=(java.lang.instrument.Instrumentation)field.get(null);
            if(instrumentation==null)throw new IllegalStateException("Recorder instrumentation unavailable");
            Map<String,Set<Module>> opens=new HashMap<>();
            for(String name:List.of("net.minecraft.client.player","net.minecraft.client.resources"))opens.put(name,Set.of(getClass().getModule()));
            if(Minecraft.class.getModule().isNamed())instrumentation.redefineModule(Minecraft.class.getModule(),Set.of(),Map.of(),opens,Set.of(),Map.of());
        }catch(ReflectiveOperationException|RuntimeException error){throw new IOException("Cannot access visible NPC profiles and loaded skin cache",error);}
    }
    private void installReceiverHook() throws IOException {
        try {
            Class<?> shell=Class.forName("local.flashback.modern.AgentV2",false,ClassLoader.getSystemClassLoader());
            Field oldHook=shell.getDeclaredField("installedHook");oldHook.setAccessible(true);
            Object previous=oldHook.get(null);
            if(!(previous instanceof java.lang.instrument.ClassFileTransformer transformer))throw new IllegalStateException("Previous packet hook unavailable");
            if(!instrumentation.removeTransformer(transformer))throw new IllegalStateException("Previous packet hook was not installed");
            ReceiverHook replacement=new ReceiverHook();receiverHook=replacement;
            instrumentation.addTransformer(replacement,true);
            instrumentation.retransformClasses(ClientPacketListener.class);
            if(replacement.methods==0||!replacement.login)throw new IllegalStateException("No verified receiver-aware LOGIN hook");
        }catch(Throwable failure){uninstallReceiverHook();throw new IOException("Cannot install receiver-aware packet hook",failure);}
    }
    private synchronized void uninstallReceiverHook() {
        ReceiverHook previous=receiverHook;receiverHook=null;
        if(previous==null||instrumentation==null)return;
        instrumentation.removeTransformer(previous);
        try{instrumentation.retransformClasses(ClientPacketListener.class);}catch(Throwable error){log("receiver hook cleanup: "+describe(error));}
    }
    private void writePackets(String file, Collection<? extends Packet<?>> values) throws IOException {
        try(DataOutputStream out=new DataOutputStream(new BufferedOutputStream(Files.newOutputStream(recordingDir.resolve(file))))) {
            for(Packet<?> packet:values){byte[] bytes=encode(playCodec,packet);out.writeInt(bytes.length);out.write(bytes);}
        }
    }

    private List<byte[]> configurationSnapshot(ClientPacketListener listener) throws Exception {
        RegistryAccess registry=listener.registryAccess();
        StreamCodec configCodec=ConfigurationProtocols.CLIENTBOUND.codec();
        List<Packet<?>> configuration=new ArrayList<>();
        configuration.add(new ClientboundUpdateEnabledFeaturesPacket(FeatureFlags.REGISTRY.toNames(listener.enabledFeatures())));
        configuration.add(new ClientboundSelectKnownPacks(List.of()));
        RegistrySynchronization.packRegistries(RegistryOps.create(NbtOps.INSTANCE,registry),registry,Set.of(),
                (key,values)->configuration.add(new ClientboundRegistryDataPacket(key,values)));
        Method serializeTags=TagNetworkSerialization.class.getDeclaredMethod("serializeToNetwork",Registry.class);serializeTags.setAccessible(true);
        Map<ResourceKey<? extends Registry<?>>,TagNetworkSerialization.NetworkPayload> tags=new LinkedHashMap<>();
        for(RegistryAccess.RegistryEntry<?> entry:registry.registries().toList()) {
            TagNetworkSerialization.NetworkPayload payload=(TagNetworkSerialization.NetworkPayload)serializeTags.invoke(null,entry.value());
            // Match vanilla: empty registries are not represented in UPDATE_TAGS.
            // A registry such as minecraft:schedule may no longer exist in the replay version.
            if(!payload.isEmpty())tags.put(entry.key(),payload);
        }
        configuration.add(new ClientboundUpdateTagsPacket(tags));
        configuration.add(ClientboundFinishConfigurationPacket.INSTANCE);
        List<byte[]> result=new ArrayList<>();
        for(Packet<?> packet:configuration)result.add(encode(configCodec,packet));
        return List.copyOf(result);
    }

    private Map<String,Object> captureSnapshot() throws Exception {
        String createdUtc=Instant.now().toString();
        ClientLevel level=mc.level; ClientPacketListener listener=mc.getConnection();
        List<byte[]> configuration=configurationSnapshot(listener);
        try(DataOutputStream out=new DataOutputStream(new BufferedOutputStream(Files.newOutputStream(recordingDir.resolve("snapshot-config.bin"))))) {
            for(byte[] bytes:configuration){out.writeInt(bytes.length);out.write(bytes);}
        }
        status.put("registrySnapshot","captured:"+configuration.size());
        GameType gameType=mc.gameMode.getPlayerMode();
        CommonPlayerSpawnInfo spawn=new CommonPlayerSpawnInfo(level.dimensionTypeRegistration(),level.dimension(),0L,gameType,
                mc.gameMode.getPreviousPlayerMode(),level.isDebug(),false,mc.player.getLastDeathLocation(),mc.player.getPortalCooldown(),level.getSeaLevel());
        int radius=((Number)field(listener,"serverChunkRadius")).intValue();
        ClientboundLoginPacket login=new ClientboundLoginPacket(mc.player.getId(),level.getLevelData().isHardcore(),listener.levels(),
                100,Math.max(2,radius),Math.max(2,radius),mc.player.isReducedDebugInfo(),true,false,spawn,false);
        Files.write(recordingDir.resolve("snapshot-packets.bin"),encode(playCodec,login));status.put("loginPacket","captured");
        // Light work is progressed using the same public client operations as a normal tick.
        for(int i=0;i<256 && !lightQueueEmpty(level);i++) level.pollLightUpdates();
        for(int i=0;i<256 && level.getLightEngine().hasLightWork();i++) level.getLightEngine().runLightUpdates();
        if(!lightQueueEmpty(level) || level.getLightEngine().hasLightWork()) throw new IllegalStateException("Lighting still pending after bounded snapshot flush");
        List<Packet<?>> chunks=new ArrayList<>();
        Object storage=field(level.getChunkSource(),"storage");
        AtomicReferenceArray<LevelChunk> loaded=(AtomicReferenceArray<LevelChunk>)field(storage,"chunks");
        for(int i=0;i<loaded.length();i++){LevelChunk chunk=loaded.get(i);if(chunk!=null)chunks.add(new ClientboundLevelChunkWithLightPacket(chunk,level.getLightEngine(),null,null));}
        if(chunks.isEmpty())throw new IllegalStateException("No loaded chunks available");
        writePackets("snapshot-chunks.bin",chunks);status.put("chunkSnapshot","captured:"+chunks.size());status.put("chunkCount",""+chunks.size());status.put("chunkSnapshotErrors","0");
        List<Packet<?>> entities=new ArrayList<>();
        entities.add(playerInfo(listener));
        int entityCount=0;
        for(Entity entity:level.entitiesForRendering()) {
            if(entity==mc.player)continue;
            entities.add(new ClientboundAddEntityPacket(entity.getId(),entity.getUUID(),entity.getX(),entity.getY(),entity.getZ(),
                    entity.getXRot(),entity.getYRot(),entity.getType(),entitySpawnData(entity),entity.getDeltaMovement(),entity.getYHeadRot()));
            entityState(entity,entities);entityCount++;
        }
        // The local player's spawn is supplied by the replay converter using the FBRL metadata/sample.
        entityState(mc.player,entities);
        writePackets("snapshot-entities.bin",entities);status.put("entitySnapshot","captured:"+entityCount);status.put("entityCount",""+entityCount);status.put("entitySnapshotErrors","0");
        List<Packet<?>> world=new ArrayList<>();
        world.add(new ClientboundSetChunkCacheRadiusPacket(Math.max(2,radius)));
        world.add(new ClientboundSetChunkCacheCenterPacket(mc.player.chunkPosition().x,mc.player.chunkPosition().z));
        world.add(new ClientboundSetTimePacket(level.getGameTime(),level.getDayTime(),true));
        world.add(new ClientboundSetDefaultSpawnPositionPacket(level.getRespawnData()));
        world.add(new ClientboundInitializeBorderPacket(level.getWorldBorder()));
        world.add(new ClientboundGameEventPacket(level.isRaining()?ClientboundGameEventPacket.START_RAINING:ClientboundGameEventPacket.STOP_RAINING,0));
        world.add(new ClientboundGameEventPacket(ClientboundGameEventPacket.RAIN_LEVEL_CHANGE,level.getRainLevel(1)));
        world.add(new ClientboundGameEventPacket(ClientboundGameEventPacket.THUNDER_LEVEL_CHANGE,level.getThunderLevel(1)));
        Scoreboard scores=level.getScoreboard();
        for(Objective objective:scores.getObjectives()) {
            world.add(new ClientboundSetObjectivePacket(objective,0));
            for(PlayerScoreEntry score:scores.listPlayerScores(objective))world.add(new ClientboundSetScorePacket(score.owner(),objective.getName(),score.value(),Optional.ofNullable(score.display()),Optional.ofNullable(score.numberFormatOverride())));
        }
        for(DisplaySlot slot:DisplaySlot.values()){Objective objective=scores.getDisplayObjective(slot);if(objective!=null)world.add(new ClientboundSetDisplayObjectivePacket(slot,objective));}
        for(PlayerTeam team:scores.getPlayerTeams())world.add(ClientboundSetPlayerTeamPacket.createAddOrModifyPacket(team,true));
        writePackets("snapshot-world.bin",world);status.put("worldSnapshot","captured:"+world.size());status.put("worldPacketCount",""+world.size());
        Files.writeString(recordingDir.resolve("capture.properties"),"chunkSnapshotErrors=0\nentitySnapshotErrors=0\nconfigPacketCount="+configuration.size()+"\n",StandardCharsets.UTF_8,StandardOpenOption.APPEND);
        Map<String,Object> meta=new LinkedHashMap<>();
        meta.put("type","meta");meta.put("format",5);meta.put("createdUtc",createdUtc);meta.put("source","runtime-modern-1.21.10");
        meta.put("version",SharedConstants.getCurrentVersion().name());meta.put("dataVersion",SharedConstants.getCurrentVersion().dataVersion().version());
        meta.put("protocolVersion",773);meta.put("worldName","Multiplayer");meta.put("playerUuid",mc.player.getUUID().toString());
        meta.put("playerName",mc.player.getGameProfile().name());meta.put("gametype",gameType.getId());meta.put("playerEntityId",mc.player.getId());
        meta.put("tickClock","monotonic-20hz");meta.put("samplingRate",sampleRate);
        for(String key:List.of("registrySnapshot","loginPacket","chunkSnapshot","entitySnapshot","worldSnapshot"))meta.put(key,status.get(key));
        meta.put("chunkCount",chunks.size());meta.put("entityCount",entityCount);meta.put("worldPacketCount",world.size());
        status.put("playerEntityId",""+mc.player.getId());return meta;
    }

    private static boolean lightQueueEmpty(ClientLevel level) throws Exception {
        // The Mojmap method became private in some distributions; read the queue without consuming it.
        try {Method method=ClientLevel.class.getDeclaredMethod("isLightUpdateQueueEmpty");method.setAccessible(true);return (boolean)method.invoke(level);}
        catch(NoSuchMethodException ignored){Object queue=field(level,"lightUpdateQueue");return ((Queue<?>)queue).isEmpty();}
    }
    private static int entitySpawnData(Entity entity) {
        if(entity instanceof net.minecraft.world.entity.item.FallingBlockEntity falling) return net.minecraft.world.level.block.Block.getId(falling.getBlockState());
        if(entity instanceof net.minecraft.world.entity.projectile.Projectile projectile && projectile.getOwner()!=null) return projectile.getOwner().getId();
        if(entity instanceof net.minecraft.world.entity.decoration.HangingEntity hanging) return hanging.getDirection().get3DDataValue();
        return 0;
    }
    private Packet<?> playerInfo(ClientPacketListener listener) throws Exception {
        var actions=EnumSet.allOf(ClientboundPlayerInfoUpdatePacket.Action.class);actions.remove(ClientboundPlayerInfoUpdatePacket.Action.INITIALIZE_CHAT);
        var packet=new ClientboundPlayerInfoUpdatePacket(actions,List.of());
        Map<UUID,ClientboundPlayerInfoUpdatePacket.Entry> entries=new LinkedHashMap<>();
        Set<UUID> listed=new HashSet<>();
        for(PlayerInfo info:listener.getListedOnlinePlayers())listed.add(info.getProfile().id());
        for(PlayerInfo info:listener.getOnlinePlayers()) {
            entries.put(info.getProfile().id(),new ClientboundPlayerInfoUpdatePacket.Entry(info.getProfile().id(),info.getProfile(),listed.contains(info.getProfile().id()),info.getLatency(),info.getGameMode(),info.getTabListDisplayName(),info.showHat(),info.getTabListOrder(),null));
        }
        int restored=0;
        for(Entity entity:mc.level.entitiesForRendering())if(entity instanceof net.minecraft.world.entity.player.Player player && player!=mc.player)
            if(PlayerProfiles.addMissing(entries,player.getUUID(),player.getGameProfile(),PlayerProfiles.cachedInfo(player),listed.contains(player.getUUID()),GameType.SURVIVAL))restored++;
        PlayerProfiles.addMissing(entries,mc.player.getUUID(),mc.player.getGameProfile(),PlayerProfiles.cachedInfo(mc.player),listed.contains(mc.player.getUUID()),mc.gameMode.getPlayerMode());
        status.put("visiblePlayerProfilesRestored",""+restored);
        setField(packet,"entries",List.copyOf(entries.values()));return packet;
    }
    private static List<SynchedEntityData.DataValue<?>> allData(Entity entity) throws Exception {
        SynchedEntityData.DataItem<?>[] values=(SynchedEntityData.DataItem<?>[])field(entity.getEntityData(),"itemsById");
        List<SynchedEntityData.DataValue<?>> result=new ArrayList<>();for(var value:values)if(value!=null)result.add(value.value());return result;
    }
    private static ClientboundSetEquipmentPacket equipment(LivingEntity entity) {
        List<Pair<EquipmentSlot,ItemStack>> slots=new ArrayList<>();
        for(EquipmentSlot slot:EquipmentSlot.values()) slots.add(Pair.of(slot,entity.getItemBySlot(slot).copy()));
        return new ClientboundSetEquipmentPacket(entity.getId(),slots);
    }
    private static void entityState(Entity entity,List<Packet<?>> packets) throws Exception {
        packets.add(new ClientboundSetEntityDataPacket(entity.getId(),allData(entity)));
        packets.add(new ClientboundRotateHeadPacket(entity,(byte)(entity.getYHeadRot()*256/360)));
        packets.add(new ClientboundSetEntityMotionPacket(entity));
        if(entity instanceof LivingEntity living) {
            packets.add(equipment(living));
            packets.add(new ClientboundUpdateAttributesPacket(entity.getId(),living.getAttributes().getSyncableAttributes()));
            for(var effect:living.getActiveEffects())packets.add(new ClientboundUpdateMobEffectPacket(entity.getId(),effect,false));
        }
        if(!entity.getPassengers().isEmpty())packets.add(new ClientboundSetPassengersPacket(entity));
    }
    private Map<String,Object> sample() throws Exception {
        if(!recording)return null;
        if(mc.player==null || mc.level==null || mc.getConnection()==null) {beginTransition("waiting-for-world");return null;}
        if(pendingLogin) {
            if(mc.getConnection()!=recordedConnection || mc.player.getId()!=sourcePlayerId || mc.level==recordedLevel || tick()<=loginTick)return null;
            transitioning=false;pendingLogin=false;transitionReason="";
            lastLocal.clear();lastLocalTick=-1;lastDestroyPos=null;lastDestroyStage=-1;
            status.put("playerEntityId",""+sourcePlayerId);
        } else if(transitioning)return null;
        if(mc.getConnection()!=recordedConnection) {beginTransition("waiting-for-new-connection-login");return null;}
        if(mc.level!=recordedLevel) {
            if(mc.player.getId()!=sourcePlayerId)throw new IllegalStateException("Player entity identity changed without a LOGIN boundary");
            recordedLevel=mc.level;lastLocal.clear();lastLocalTick=-1;
            lastDestroyPos=null;lastDestroyStage=-1;
            playCodec=GameProtocols.CLIENTBOUND_TEMPLATE.bind(RegistryFriendlyByteBuf.decorator(mc.getConnection().registryAccess())).codec();
        }
        var player=mc.player;long tick=tick();
        samplingTick=tick;
        try {
        if(tick!=lastLocalTick) {
            lastLocalTick=tick;
            for(int slot=0;slot<9;slot++)changedLocal("hotbar-"+slot,new ClientboundSetPlayerInventoryPacket(slot,player.getInventory().getItem(slot).copy()));
            changedLocal("held",new ClientboundSetHeldSlotPacket(player.getInventory().getSelectedSlot()));
            changedLocal("experience",new ClientboundSetExperiencePacket(player.experienceProgress,player.totalExperience,player.experienceLevel));
            changedLocal("health",new ClientboundSetHealthPacket(player.getHealth(),player.getFoodData().getFoodLevel(),player.getFoodData().getSaturationLevel()));
            changedLocal("equipment",equipment(player));
            changedLocal("metadata",new ClientboundSetEntityDataPacket(player.getId(),allData(player)));
            changedLocal("attributes",new ClientboundUpdateAttributesPacket(player.getId(),player.getAttributes().getSyncableAttributes()));
            queue(ClientboundEntityPositionSyncPacket.of(player));
            queue(new ClientboundRotateHeadPacket(player,(byte)(player.getYHeadRot()*256/360)));
            if(player.swinging && (player.swingTime<oldSwingTime || oldSwingTime==0)) {
                queue(new ClientboundAnimatePacket(player,player.swingingArm==net.minecraft.world.InteractionHand.MAIN_HAND?0:3));localAnimations++;
            }
            oldSwingTime=player.swinging?player.swingTime:0;
            captureDestruction();
            if(tick>=nextSkinTick){captureSkins();nextSkinTick=tick+20;}
            remoteState=new ArrayList<>();
            for(Entity entity:mc.level.entitiesForRendering())if(entity!=player) {
                Map<String,Object> remote=new LinkedHashMap<>();remote.put("id",entity.getId());remote.put("x",entity.getX());remote.put("y",entity.getY());remote.put("z",entity.getZ());
                remote.put("yaw",entity.getYRot());remote.put("pitch",entity.getXRot());remote.put("headYaw",entity.getYHeadRot());remote.put("onGround",entity.onGround());remoteState.add(remote);
            }
        }
        Map<String,Object> sample=new LinkedHashMap<>();sample.put("type","sample");sample.put("t",elapsedMillis());sample.put("tick",tick);sample.put("playerEntityId",player.getId());
        sample.put("gameTick",player.tickCount);sample.put("dimension",mc.level.dimension().location().toString());
        sample.put("x",player.getX());sample.put("y",player.getY());sample.put("z",player.getZ());sample.put("yaw",player.getYRot());sample.put("pitch",player.getXRot());
        sample.put("velX",player.getDeltaMovement().x);sample.put("velY",player.getDeltaMovement().y);sample.put("velZ",player.getDeltaMovement().z);
        sample.put("headYaw",player.getYHeadRot());sample.put("onGround",player.onGround());sample.put("entities",remoteState);return sample;
        } finally {samplingTick=-1;}
    }
    private void changedLocal(String name,Packet<?> packet) {
        try {byte[] bytes=encode(playCodec,packet);if(!Arrays.equals(lastLocal.get(name),bytes)){lastLocal.put(name,bytes);queueBytes(bytes);}}
        catch(Throwable error){packetError("local-"+name,error);}
    }
    private void captureDestruction() throws Exception {
        if(mc.gameMode==null)return;
        BlockPos pos=(BlockPos)field(mc.gameMode,"destroyBlockPos");int stage=mc.gameMode.isDestroying()?mc.gameMode.getDestroyStage():-1;
        if(stage==lastDestroyStage && (stage<0 || Objects.equals(pos,lastDestroyPos)))return;
        BlockPos write=stage<0 && lastDestroyPos!=null?lastDestroyPos:pos;
        if(write==null)return;
        write=write.immutable();queue(new ClientboundBlockDestructionPacket(mc.player.getId(),write,stage));localDestruction++;
        lastDestroyPos=write;lastDestroyStage=stage;
    }
    private void captureEffect(Object[] event){
        if(!hasEffects || mc.player==null || mc.level==null || event.length<2)return;
        try {
            int kind=((Number)event[0]).intValue();
            if(kind!=4 && event[1]!=mc.level)return;
            // Incoming server effects are already serialized by the packet hook. Avoid
            // recording the local sound/particles generated while that handler runs.
            boolean replicated=StackWalker.getInstance().walk(frames->frames.anyMatch(frame ->
                    frame.getClassName().equals("net.minecraft.client.multiplayer.ClientPacketListener") ||
                    ((kind==0 || kind==4) && frame.getClassName().equals("net.minecraft.client.multiplayer.ClientLevel") &&
                     (frame.getMethodName().equals("levelEvent") || frame.getMethodName().equals("globalLevelEvent")))));
            if(replicated)return;
            if(kind==0){
                queue(new ClientboundSoundPacket(Holder.direct((net.minecraft.sounds.SoundEvent)event[5]),(net.minecraft.sounds.SoundSource)event[6],
                        ((Number)event[2]).doubleValue(),((Number)event[3]).doubleValue(),((Number)event[4]).doubleValue(),
                        ((Number)event[7]).floatValue(),((Number)event[8]).floatValue(),((Number)event[10]).longValue()));localSounds++;
            }else if(kind==1 || kind==2){
                int offset=kind==1?3:2;queue(new ClientboundLevelEventPacket(((Number)event[offset]).intValue(),(BlockPos)event[offset+1],((Number)event[offset+2]).intValue(),kind==2));localEvents++;
            }else if(kind==3){
                Object prediction=field(mc.level,"blockStatePredictionHandler");
                if(prediction.getClass().getMethod("isPredicting").invoke(prediction).equals(Boolean.TRUE)){
                    queue(new ClientboundBlockUpdatePacket(((BlockPos)event[2]).immutable(),(net.minecraft.world.level.block.state.BlockState)event[3]));localPredictions++;
                }
            }else if(kind==4){
                var sound=(net.minecraft.client.resources.sounds.SoundInstance)event[1];if(sound.getSound()==null)return;
                queue(new ClientboundSoundPacket(Holder.direct(net.minecraft.sounds.SoundEvent.createVariableRangeEvent(sound.getLocation())),sound.getSource(),
                        sound.getX(),sound.getY(),sound.getZ(),sound.getVolume(),sound.getPitch(),ThreadLocalRandom.current().nextLong()));localSounds++;
            }
        }catch(Throwable error){packetError("local-effect",error);}
    }
    private void captureSkins(){
        if(skins==null)return;
        try{pendingSkins=skins.snapshot(mc);}catch(Exception error){skinErrors++;skinError=describe(error);}
    }
    private void flushSkins(){
        RecordedSkinCapture.Batch batch=pendingSkins;pendingSkins=null;
        if(batch!=null && skinWriter!=null)skinWriter.write(batch,message->skinError=message);
    }
}
