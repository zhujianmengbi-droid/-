package local.flashback.modern.impl;

import com.mojang.authlib.GameProfile;
import java.lang.reflect.Field;
import java.util.*;
import net.minecraft.client.multiplayer.PlayerInfo;
import net.minecraft.network.protocol.game.ClientboundPlayerInfoUpdatePacket;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.level.GameType;

/** A visible NPC may retain its PlayerInfo after the server removes it from the Tab list. */
final class PlayerProfiles {
    static PlayerInfo cachedInfo(Player player) {
        for(Class<?> type=player.getClass();type!=null;type=type.getSuperclass())try {
            Field field=type.getDeclaredField("playerInfo");field.setAccessible(true);
            return (PlayerInfo)field.get(player);
        }catch(NoSuchFieldException ignored){}catch(ReflectiveOperationException|RuntimeException unavailable){return null;}
        return null;
    }
    static GameProfile actualProfile(UUID entityId,GameProfile entity,GameProfile cached) {
        if(entity==null || entity.name()==null || entity.name().isBlank())throw new IllegalArgumentException("Visible player has no real profile name");
        GameProfile source=entity;
        if(entity.properties().get("textures").isEmpty() && cached!=null && entityId.equals(cached.id())
                && entity.name().equals(cached.name()) && !cached.properties().get("textures").isEmpty())source=cached;
        return entityId.equals(source.id())?source:new GameProfile(entityId,entity.name(),source.properties());
    }
    static boolean addMissing(Map<UUID,ClientboundPlayerInfoUpdatePacket.Entry> entries,UUID entityId,
            GameProfile entity,PlayerInfo cached,boolean listed,GameType fallback) {
        if(entries.containsKey(entityId))return false;
        GameProfile profile=actualProfile(entityId,entity,cached==null?null:cached.getProfile());
        GameType gameMode=cached==null?fallback:cached.getGameMode();
        entries.put(entityId,new ClientboundPlayerInfoUpdatePacket.Entry(entityId,profile,listed,
                cached==null?0:cached.getLatency(),gameMode==null?fallback:gameMode,
                cached==null?null:cached.getTabListDisplayName(),cached==null||cached.showHat(),cached==null?0:cached.getTabListOrder(),null));
        return true;
    }
}
