package local.flashback.modern.impl;

import java.io.IOException;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.function.Consumer;
import java.util.concurrent.CompletableFuture;
import com.google.common.cache.Cache;
import com.google.common.hash.Hashing;
import com.google.gson.*;
import com.mojang.authlib.GameProfile;
import com.mojang.authlib.properties.Property;
import com.mojang.authlib.minecraft.MinecraftProfileTextures;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.PlayerInfo;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.player.PlayerSkin;

/** Copies already cached ordinary skins; never invokes lazy skin loading or requests a URL. */
public final class RecordedSkinCapture {
    public record Candidate(String uuid, String name, String path, String model, String sourceKey) {
        public Candidate(String uuid,String name,String path,String model){this(uuid,name,path,model,"netease");}
    }
    public record Batch(List<String> allowedDirectories, List<Candidate> players, Map<String,String> authoritativeProfiles) {
        public Batch { allowedDirectories=List.copyOf(allowedDirectories);players = List.copyOf(players);authoritativeProfiles=Map.copyOf(authoritativeProfiles); }
    }
    private record Player(String uuid, String name, GameProfile profile) {}
    record Texture(String hash,String model) {}
    private final Class<?> skinHandler;
    private final Map<String,Player> seen = new LinkedHashMap<>();
    public RecordedSkinCapture(ClassLoader loader) {
        Class<?> found=null;try{found=Class.forName("com.netease.mc.mod.skin.SkinHandler",false,loader);}catch(ClassNotFoundException ignored){}
        skinHandler=found;
    }
    public boolean available() { return true; }
    public void addRequestedPlayer(String uuid,String name) {
        UUID.fromString(uuid);if(name==null||name.isBlank())return;
        seen.put(uuid,new Player(uuid,name,new GameProfile(UUID.fromString(uuid),name)));
    }
    /** Observe ADD_PLAYER profiles at the existing packet hook, even when an entity leaves within one polling interval. */
    public void observePlayerInfo(Object packet)throws Exception {
        Object entries=invoke(packet,"entries","m_246778_");
        if(entries instanceof Iterable<?> list)for(Object entry:list){
            Object profile=invoke(entry,"profile","f_243688_");
            rememberProfile(profile);
        }
    }

    /** Must run on Minecraft's client thread. Returns only immutable strings, with no file IO. */
    public Batch snapshot(Object minecraft) throws Exception {
        return snapshot(minecraft,true);
    }
    public Batch snapshot(Object minecraft,boolean discoverVisiblePlayers) throws Exception {
        Minecraft mc=(Minecraft)minecraft;
        List<String> allowed=new ArrayList<>();
        Map<?,?> pathMap=Map.of(),modeMap=Map.of();
        if(skinHandler!=null) {
            Object configured=staticField(skinHandler,"assetSkinsDir");
            if(configured instanceof java.io.File folder)allowed.add(folder.toPath().toAbsolutePath().normalize().toString());
            Object paths=staticField(skinHandler,"nameSkinMap"),modes=staticField(skinHandler,"nameSkinMode");
            if(paths instanceof Map<?,?> map)pathMap=map;
            if(modes instanceof Map<?,?> map)modeMap=map;
        }
        if(discoverVisiblePlayers){
            remember(mc.player);
            if(mc.level!=null)for(Entity entity:mc.level.entitiesForRendering())remember(entity);
            if(mc.getConnection()!=null)for(PlayerInfo info:mc.getConnection().getOnlinePlayers())rememberProfile(info.getProfile());
        }
        Object manager=mc.getSkinManager();
        Object textureCache=field(manager,"skinTextures");
        Path textureRoot=(Path)field(textureCache,"root");
        textureRoot=textureRoot.toAbsolutePath().normalize();allowed.add(textureRoot.toString());
        Cache<?,?> cache=(Cache<?,?>)field(manager,"skinCache");
        // asMap/getNow reads existing completed results; get/getUnchecked/getSkin would initiate loading.
        Map<?,?> loaded=cache.asMap();
        Cache<?,?> neteaseCache=skinHandler==null?null:(Cache<?,?>)staticField(skinHandler,"skinCacheLoader");
        List<Candidate> players=new ArrayList<>();
        Map<String,String> authoritative=new HashMap<>();
        for(Player player:seen.values()){
            Collection<Property> properties=player.profile.properties().get("textures");
            String sourceKey=profileKey(properties);
            if(sourceKey!=null)authoritative.put(player.uuid,sourceKey);
            Candidate resolved=cachedNeteaseSkin(player,textureRoot,neteaseCache);
            if(resolved!=null){authoritative.put(player.uuid,resolved.sourceKey);players.add(resolved);continue;}
            resolved=cachedSkin(player,textureRoot,loaded);
            if(resolved!=null){players.add(resolved);continue;}
            if(!properties.isEmpty()) {
                for(Property property:properties){Texture texture=profileTexture(property);
                    if(texture!=null){players.add(new Candidate(player.uuid,player.name,cachePath(textureRoot,texture.hash).toString(),texture.model,sourceKey));break;}}
                // Never overwrite a server profile with the launcher's default/placeholder skin.
                // If its file is not cached yet, the replay retains the untouched textures property.
                continue;
            }
            Object source=pathMap.get(player.name);
            if(!(source instanceof String path)||path.isBlank())continue;
            Object mapped=modeMap.get(player.name);
            String model=mapped instanceof Boolean slim?(slim?"slim":"wide"):null;
            // No width guess from PNG dimensions: both models commonly use 64x64.
            if(model==null)continue;
            players.add(new Candidate(player.uuid,player.name,path,model));
        }
        return new Batch(allowed,players,authoritative);
    }
    private static Candidate cachedNeteaseSkin(Player player,Path root,Cache<?,?> cache) {
        // NetEase replaces SkinManager.get. Its completed profile-keyed cache is what
        // the renderer actually registers; the nameSkinMap can still contain a placeholder.
        Object found=cache==null?null:cache.getIfPresent(player.profile);
        if(!(found instanceof MinecraftProfileTextures textures)||textures.skin()==null)return null;
        String hash=textures.skin().getHash();
        if(!hash.matches("[0-9a-fA-F]{32,128}"))return null;
        String model="slim".equals(textures.skin().getMetadata("model"))?"slim":"wide";
        return new Candidate(player.uuid,player.name,cachePath(root,hash).toString(),model,"texture:"+hash+":"+model);
    }
    private void remember(Object entity)throws Exception{
        if(!(entity instanceof net.minecraft.world.entity.player.Player player))return;
        PlayerInfo cached=PlayerProfiles.cachedInfo(player);
        rememberProfile(PlayerProfiles.actualProfile(player.getUUID(),player.getGameProfile(),cached==null?null:cached.getProfile()));
    }
    private void rememberProfile(Object gp)throws Exception{
        if(!(gp instanceof GameProfile profile)||profile.name()==null||profile.name().isBlank())return;
        String uuid=profile.id().toString();
        Player old=seen.get(uuid);
        if(old!=null && old.name.equals(profile.name()) && profile.properties().get("textures").isEmpty() && !old.profile.properties().get("textures").isEmpty())return;
        seen.put(uuid,new Player(uuid,profile.name(),profile));
    }
    private static Candidate cachedSkin(Player player,Path root,Map<?,?> cache)throws Exception {
        Collection<Property> textures=player.profile.properties().get("textures");
        for(var entry:cache.entrySet()) {
            Object key=entry.getKey();
            if(!UUID.fromString(player.uuid).equals(field(key,"profileId")))continue;
            Object packed=field(key,"packedTextures");
            if(packed==null?!textures.isEmpty():!textures.contains(packed))continue;
            if(!(entry.getValue() instanceof CompletableFuture<?> future)||!future.isDone()||future.isCompletedExceptionally()||future.isCancelled())continue;
            Object value=future.getNow(null);
            if(!(value instanceof Optional<?> optional)||!(optional.orElse(null) instanceof PlayerSkin skin))continue;
            String resource=skin.body().texturePath().toString();
            if(!resource.matches("minecraft:skins/[0-9a-f]{40}"))continue;
            String storage=resource.substring("minecraft:skins/".length());
            String sourceKey=profileKey(textures);
            return new Candidate(player.uuid,player.name,root.resolve(storage.substring(0,2)).resolve(storage).toString(),skin.model().getSerializedName(),sourceKey==null?"resolved:"+resource:sourceKey);
        }
        return null;
    }
    static String profileKey(Collection<Property> textures) {
        if(textures.isEmpty())return null;
        for(Property property:textures) {
            Texture texture=profileTexture(property);
            if(texture!=null)return "texture:"+texture.hash+":"+texture.model;
        }
        // Invalid properties still suppress an unsafe launcher placeholder, but valid
        // ones are keyed by image/model, not reissued timestamps or signatures.
        String value=textures.stream().map(Property::value).sorted().reduce("",(left,right)->left+right.length()+":"+right);
        return "profile:"+Hashing.sha256().hashString(value,StandardCharsets.UTF_8);
    }
    static Texture profileTexture(Property property) {
        try {
            if(property.value().length()>131072)return null;
            JsonObject value=JsonParser.parseString(new String(Base64.getDecoder().decode(property.value()),StandardCharsets.UTF_8)).getAsJsonObject();
            JsonObject textures=value.getAsJsonObject("textures");if(textures==null)return null;
            JsonObject skin=textures.getAsJsonObject("SKIN");if(skin==null)return null;
            String url=skin.get("url").getAsString();
            java.net.URI uri=java.net.URI.create(url);
            if(!"http".equalsIgnoreCase(uri.getScheme())&&!"https".equalsIgnoreCase(uri.getScheme()))return null;
            String path=uri.getPath();String hash=path.substring(path.lastIndexOf('/')+1);
            if(!hash.matches("[0-9a-fA-F]{32,128}"))return null;
            JsonObject metadata=skin.getAsJsonObject("metadata");
            String model=metadata!=null&&metadata.has("model")&&"slim".equals(metadata.get("model").getAsString())?"slim":"wide";
            return new Texture(hash,model);
        }catch(RuntimeException malformed){return null;}
    }
    static Path cachePath(Path root,String textureHash) {
        String storage=Hashing.sha1().hashUnencodedChars(textureHash).toString();
        return root.resolve(storage.substring(0,2)).resolve(storage);
    }
    private static Object staticField(Class<?> type,String name)throws Exception {Field f=type.getDeclaredField(name);f.setAccessible(true);return f.get(null);}
    private static Object field(Object target,String... names)throws ReflectiveOperationException{
        for(Class<?> type=target.getClass();type!=null;type=type.getSuperclass())for(String name:names)try{
            Field f=type.getDeclaredField(name);f.setAccessible(true);return f.get(target);
        }catch(NoSuchFieldException ignored){}return null;
    }
    private static Object invoke(Object target,String... names)throws Exception{return invoke(target,new Class<?>[0],new Object[0],names);}
    private static Object invoke(Object target,Class<?>[] types,Object[] args,String... names)throws Exception{
        Method method=method(target.getClass(),types,names);return method==null?null:method.invoke(target,args);
    }
    private static Method method(Class<?> type,Class<?>[] args,String... names){
        for(Class<?> cursor=type;cursor!=null;cursor=cursor.getSuperclass())for(String name:names)try{
            Method m=cursor.getDeclaredMethod(name,args);m.setAccessible(true);return m;
        }catch(NoSuchMethodException ignored){}return null;
    }

    /** Owned by the recorder worker; do not call on the client/render thread. */
    public static final class Writer {
        private static final int LIMIT=8*1024*1024;
        private final Path root;
        private final Map<String,Entry> entries=new LinkedHashMap<>();
        private final Map<String,String> copied=new HashMap<>();
        private final Set<String> failures=new HashSet<>();
        private int errors;
        private boolean publishNeeded;
        private record Entry(String uuid,String name,String file,String model,String sha256,String sourceKey){}
        public Writer(Path directory)throws IOException{
            root=directory.toAbsolutePath().normalize();Files.createDirectories(root);
            if(Files.exists(root.resolve("recorded-skins.json")))throw new FileAlreadyExistsException("Skin manifest already exists; use a fresh recording/probe directory");
        }
        public int count(){return entries.size();}
        public int errors(){return errors;}
        public void write(Batch batch,Consumer<String> failure){
            Map<String,Entry> nextEntries=new LinkedHashMap<>(entries);
            // Replace only conflicting overrides. Leaving the world or losing an on-disk cache
            // never discards an already copied skin for the same authoritative texture.
            for(var authority:batch.authoritativeProfiles.entrySet()) {
                Entry previous=nextEntries.get(authority.getKey());
                if(previous!=null&&!previous.sourceKey.equals(authority.getValue()))nextEntries.remove(authority.getKey());
            }
            for(Candidate player:batch.players())try{
                if(!player.model.equals("slim")&&!player.model.equals("wide"))throw new IOException("Invalid arm model");
                UUID.fromString(player.uuid);
                Path requested=Path.of(player.path);if(!Files.isRegularFile(requested))continue;
                Path source=requested.toRealPath();
                boolean skinCache=false;
                for(String directory:batch.allowedDirectories)if(Files.isDirectory(Path.of(directory))&&source.startsWith(Path.of(directory).toRealPath())){skinCache=true;break;}
                boolean launcherCache=false;
                for(Path parent=source.getParent();parent!=null && parent.getParent()!=null;parent=parent.getParent())
                    if(parent.getFileName().toString().equalsIgnoreCase("skin") && parent.getParent().getFileName()!=null && parent.getParent().getFileName().toString().equalsIgnoreCase("cache")){launcherCache=true;break;}
                if(!skinCache && (!launcherCache||!source.getFileName().toString().toLowerCase(Locale.ROOT).endsWith(".png")))
                    throw new IOException("Skin path outside NetEase skin cache");
                long size=Files.size(source);if(size<33||size>LIMIT)throw new IOException("Skin PNG size outside limit");
                String fingerprint=source+":"+size+":"+Files.getLastModifiedTime(source).toMillis();
                String hash=copied.get(fingerprint);
                if(hash==null){byte[] bytes;try(var in=Files.newInputStream(source)){bytes=in.readNBytes(LIMIT+1);}
                    if(bytes.length>LIMIT)throw new IOException("Skin PNG grew beyond limit");
                    validatePng(bytes);hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
                    Path output=root.resolve("recorded-skins").resolve(hash+".png");Files.createDirectories(output.getParent());
                    if(!Files.exists(output))Files.write(output,bytes,StandardOpenOption.CREATE_NEW);
                    copied.put(fingerprint,hash);
                }
                Entry next=new Entry(player.uuid,player.name,"recorded-skins/"+hash+".png",player.model,hash,player.sourceKey);
                nextEntries.put(player.uuid,next);
            }catch(Exception error){String key=player.uuid+":"+error.getClass().getName()+":"+error.getMessage();if(failures.add(key)){errors++;failure.accept("skin "+player.uuid+": "+error.getMessage());}}
            if(!nextEntries.equals(entries)){entries.clear();entries.putAll(nextEntries);publishNeeded=true;}
            if(publishNeeded)try{publish();publishNeeded=false;}catch(IOException error){errors++;failure.accept("skin manifest: "+error.getMessage());}
        }
        private void publish()throws IOException{
            StringBuilder json=new StringBuilder("{\"version\":1,\"players\":[");boolean first=true;
            for(Entry e:entries.values()){
                if(!first)json.append(',');first=false;json.append("{\"uuid\":").append(quote(e.uuid)).append(",\"name\":").append(quote(e.name))
                    .append(",\"file\":").append(quote(e.file)).append(",\"model\":").append(quote(e.model)).append(",\"sha256\":").append(quote(e.sha256)).append('}');}
            json.append("]}\n");Path temporary=Files.createTempFile(root,".recorded-skins-",".tmp");
            try{Files.writeString(temporary,json,StandardCharsets.UTF_8);try{Files.move(temporary,root.resolve("recorded-skins.json"),StandardCopyOption.REPLACE_EXISTING,StandardCopyOption.ATOMIC_MOVE);}
                catch(AtomicMoveNotSupportedException unsupported){Files.move(temporary,root.resolve("recorded-skins.json"),StandardCopyOption.REPLACE_EXISTING);}}
            finally{Files.deleteIfExists(temporary);}
        }
        static void validatePng(byte[] b)throws IOException{
            byte[] signature={(byte)137,80,78,71,13,10,26,10};if(b.length<33)throw new IOException("Truncated PNG");
            for(int i=0;i<8;i++)if(b[i]!=signature[i])throw new IOException("Not a PNG");
            if(b[12]!='I'||b[13]!='H'||b[14]!='D'||b[15]!='R')throw new IOException("Missing PNG IHDR");
            int w=intAt(b,16),h=intAt(b,20);if(w<64||w>2048||w%64!=0||!(h==w||h*2==w))throw new IOException("Not an ordinary player skin size: "+w+"x"+h);
        }
        private static int intAt(byte[] b,int offset){return ((b[offset]&255)<<24)|((b[offset+1]&255)<<16)|((b[offset+2]&255)<<8)|(b[offset+3]&255);}
    }
    private static String quote(String text){StringBuilder out=new StringBuilder("\"");for(char c:text.toCharArray())switch(c){case '"'->out.append("\\\"");case '\\'->out.append("\\\\");case '\n'->out.append("\\n");case '\r'->out.append("\\r");case '\t'->out.append("\\t");default->{if(c<32)out.append(String.format(Locale.ROOT,"\\u%04x",(int)c));else out.append(c);}}return out.append('"').toString();}
}
