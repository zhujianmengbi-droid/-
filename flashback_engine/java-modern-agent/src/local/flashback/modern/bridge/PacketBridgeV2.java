package local.flashback.modern.bridge;

import java.util.function.Consumer;

/** Bootstrap-visible callback. Never changes packet arguments or game results. */
public final class PacketBridgeV2 {
    public static volatile Consumer<Object> sink;
    public static void capture(Object packet) {
        Consumer<Object> target = sink;
        if (target != null) try { target.accept(packet); } catch (Throwable ignored) {}
    }
    public static void hitSound(Object sound){capture(new Object[]{4,sound});}
    public static void captureSuccessful(Object event,boolean successful){if(successful)capture(event);}
}
