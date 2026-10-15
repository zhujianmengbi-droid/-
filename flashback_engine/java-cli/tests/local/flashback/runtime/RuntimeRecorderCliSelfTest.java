package local.flashback.runtime;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.jar.*;

/** Offline checks: no test reaches VirtualMachine.attach. */
public final class RuntimeRecorderCliSelfTest {
    private static int checks;
    public static void main(String[] args) throws Exception {
        Path root = Path.of(args[0]).toAbsolutePath().normalize().resolve(UUID.randomUUID().toString());
        Files.createDirectories(root);
        Path state = root.resolve("state 录制");
        Path modern = fixture(root.resolve("modern renamed with spaces.jar"), "local.flashback.modern.Agent");
        Path legacy = fixture(root.resolve("runtime-recorder-agent-1.21.10-v1.jar"), "local.flashback.runtime.v4.RuntimeRecorderAgent");
        check(RuntimeRecorderCli.installOptions(modern, state).equals("state=" + state + ",implementation=" + modern), "modern manifest selects exact absolute implementation");
        Path modernV2 = fixture(root.resolve("modern-v2-renamed.jar"), "local.flashback.modern.AgentV2");
        check(RuntimeRecorderCli.installOptions(modernV2, state).equals("state=" + state + ",implementation=" + modernV2), "AgentV2 manifest receives exact implementation override");
        Path untrustedSuffix = fixture(root.resolve("AgentV2.jar"), "local.flashback.modern.AgentV20");
        check(RuntimeRecorderCli.installOptions(untrustedSuffix, state).equals("state=" + state), "agent entry allowlist does not accept prefixes");
        Path untrustedCase = fixture(root.resolve("AgentV2-case.jar"), "local.flashback.modern.agentV2");
        check(RuntimeRecorderCli.installOptions(untrustedCase, state).equals("state=" + state), "agent entry allowlist is case sensitive");
        check(RuntimeRecorderCli.installOptions(legacy, state).equals("state=" + state), "legacy options remain unchanged despite misleading filename");
        check(!Files.exists(state), "option construction has no session-directory side effects");
        check(RuntimeRecorderCli.installOptions(root.resolve("." + File.separator + modern.getFileName()), root.resolve("child/../state 录制"))
                .equals("state=" + state + ",implementation=" + modern), "paths are normalized before options are built");
        Path other = fixture(root.resolve("other.jar"), "some.other.Agent");
        check(RuntimeRecorderCli.installOptions(other, state).equals("state=" + state), "unknown agent classes do not receive modern-only options");
        Path emptyManifest = fixture(root.resolve("no-agent.jar"), null);
        check(RuntimeRecorderCli.installOptions(emptyManifest, state).equals("state=" + state), "missing Agent-Class does not misidentify modern agent");
        Path noManifest = root.resolve("no-manifest.jar");
        try (JarOutputStream ignored = new JarOutputStream(Files.newOutputStream(noManifest))) { }
        check(RuntimeRecorderCli.installOptions(noManifest, state).equals("state=" + state), "missing manifest keeps legacy option shape");
        reject(() -> RuntimeRecorderCli.installOptions(modern, root.resolve("bad,state")), "comma in modern state rejected");
        reject(() -> RuntimeRecorderCli.installOptions(legacy, root.resolve("bad,state")), "comma in legacy state rejected");
        Path commaModern = fixture(root.resolve("modern,copy.jar"), "local.flashback.modern.Agent");
        reject(() -> RuntimeRecorderCli.installOptions(commaModern, state), "comma in implementation path rejected");
        Path commaModernV2 = fixture(root.resolve("modern-v2,copy.jar"), "local.flashback.modern.AgentV2");
        reject(() -> RuntimeRecorderCli.installOptions(commaModernV2, state), "comma in AgentV2 implementation path rejected");
        Path commaLegacy = fixture(root.resolve("legacy,copy.jar"), "local.flashback.runtime.v4.RuntimeRecorderAgent");
        check(RuntimeRecorderCli.installOptions(commaLegacy, state).equals("state=" + state), "legacy JAR path is not encoded into comma-delimited options");
        reject(() -> RuntimeRecorderCli.main(new String[]{"install", "999999999", modern.toString(), root.resolve("bad,state").toString()}), "install rejects comma options before attach");
        check(!Files.exists(root.resolve("bad,state")), "invalid install creates no state directory");
        Files.createDirectories(state);
        Files.writeString(state.resolve("status.properties"), "attached=true\nstate=ready\n", StandardCharsets.UTF_8);
        reject(() -> RuntimeRecorderCli.main(new String[]{"install", "999999999", modern.toString(), state.toString()}), "existing status blocks install before attach");
        RuntimeRecorderCli.main(new String[]{"command", state.toString(), "start:60"});
        check(Files.readString(state.resolve("command.txt")).equals("start:60" + System.lineSeparator()), "existing start-rate command interface retained");
        RuntimeRecorderCli.main(new String[]{"command", state.toString(), "mark:测试", "with spaces"});
        check(Files.readString(state.resolve("command.txt")).equals("mark:测试 with spaces" + System.lineSeparator()), "Unicode marker arguments retain existing join behavior");
        try (var files = Files.list(state)) { check(files.noneMatch(p -> p.getFileName().toString().endsWith(".tmp")), "atomic command write leaves no temporary file"); }
        ByteArrayOutputStream captured = new ByteArrayOutputStream();
        PrintStream original = System.out;
        try (PrintStream replacement = new PrintStream(captured, true, StandardCharsets.UTF_8)) {
            System.setOut(replacement);
            RuntimeRecorderCli.main(new String[]{"status", state.toString()});
        } finally { System.setOut(original); }
        check(captured.toString(StandardCharsets.UTF_8).equals("attached=true\nstate=ready\n"), "status interface remains byte-for-byte text compatible");
        Path runtime = Path.of(args[1]).toAbsolutePath().normalize();
        check(RuntimeRecorderCli.installOptions(runtime.resolve("runtime-recorder-agent-v13.jar"), state).equals("state=" + state), "actual v13 JAR receives original options");
        Path actualModern = runtime.resolve("runtime-recorder-agent-1.21.10-v2.jar");
        check(RuntimeRecorderCli.installOptions(actualModern, state).equals("state=" + state + ",implementation=" + actualModern), "actual modern JAR receives implementation override");
        try (JarFile cli = new JarFile(runtime.resolve("runtime-recorder-cli.jar").toFile())) {
            check("local.flashback.runtime.RuntimeRecorderCli".equals(cli.getManifest().getMainAttributes().getValue("Main-Class")), "CLI manifest main class is unchanged");
            byte[] bytes = cli.getInputStream(cli.getJarEntry("local/flashback/runtime/RuntimeRecorderCli.class")).readAllBytes();
            check(((bytes[6] & 255) << 8 | bytes[7] & 255) == 61, "CLI bytecode remains Java 17 compatible");
        }
        System.out.println(checks + " checks passed; no game attach was attempted.");
    }
    private static Path fixture(Path path, String agentClass) throws IOException {
        Manifest manifest = new Manifest();
        manifest.getMainAttributes().putValue("Manifest-Version", "1.0");
        if (agentClass != null) manifest.getMainAttributes().putValue("Agent-Class", agentClass);
        try (JarOutputStream ignored = new JarOutputStream(Files.newOutputStream(path), manifest)) { }
        return path;
    }
    private static void check(boolean condition, String name) {
        if (!condition) throw new AssertionError(name);
        checks++; System.out.println("PASS " + name);
    }
    private static void reject(ThrowingAction action, String name) throws Exception {
        try { action.run(); } catch (IllegalArgumentException expected) { check(true, name); return; }
        throw new AssertionError("Expected rejection: " + name);
    }
    @FunctionalInterface private interface ThrowingAction { void run() throws Exception; }
}
