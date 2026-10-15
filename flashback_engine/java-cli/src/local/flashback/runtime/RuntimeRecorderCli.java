package local.flashback.runtime;

import com.sun.tools.attach.VirtualMachine;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.jar.JarFile;
import java.util.jar.Manifest;

/** Installs the post-start runtime recorder and controls it through a local command file. */
public final class RuntimeRecorderCli {
    public static void main(String[] args) throws Exception {
        if (args.length < 1) { usage(); System.exit(2); }
        String command = args[0].toLowerCase(java.util.Locale.ROOT);
        if (command.equals("install")) {
            if (args.length != 4 || !args[1].matches("[1-9][0-9]*")) { usage(); System.exit(2); }
            Path agent = Path.of(args[2]).toAbsolutePath().normalize();
            Path state = Path.of(args[3]).toAbsolutePath().normalize();
            // Validate the comma-delimited option payload before creating a
            // session or attaching. The loaded entry point can come from an
            // older JAR, so pass this exact modern implementation explicitly.
            String options = installOptions(agent, state);
            Files.createDirectories(state);
            Path status = state.resolve("status.properties");
            if (Files.exists(status)) throw new IllegalArgumentException("State directory already has a status file; choose a new directory.");
            VirtualMachine vm = VirtualMachine.attach(args[1]);
            try { vm.loadAgent(agent.toString(), options); }
            finally { vm.detach(); }
            System.out.println("attached state=" + state);
            return;
        }
        if (command.equals("command")) {
            if (args.length < 3) { usage(); System.exit(2); }
            Path state = Path.of(args[1]).toAbsolutePath().normalize();
            String value = String.join(" ", java.util.Arrays.copyOfRange(args, 2, args.length));
            Path target = state.resolve("command.txt");
            Path temp = Files.createTempFile(state, "command-", ".tmp");
            Files.writeString(temp, value + System.lineSeparator(), StandardCharsets.UTF_8, StandardOpenOption.TRUNCATE_EXISTING);
            try { Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE); }
            catch (java.nio.file.AtomicMoveNotSupportedException ignored) { Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING); }
            System.out.println("command=" + value + " state=" + state);
            return;
        }
        if (command.equals("status")) {
            if (args.length != 2) { usage(); System.exit(2); }
            Path status = Path.of(args[1]).toAbsolutePath().normalize().resolve("status.properties");
            if (!Files.isRegularFile(status)) throw new IOException("No status file; install first.");
            System.out.print(Files.readString(status, StandardCharsets.UTF_8));
            return;
        }
        usage(); System.exit(2);
    }

    // Package-visible for an offline regression test; this never attaches or
    // creates directories. Agent identity comes from its manifest, not a name.
    static String installOptions(Path agentPath, Path statePath) throws IOException {
        Path agent = agentPath.toAbsolutePath().normalize();
        Path state = statePath.toAbsolutePath().normalize();
        rejectOptionDelimiter(state, "State directory");
        boolean modern;
        try (JarFile jar = new JarFile(agent.toFile())) {
            Manifest manifest = jar.getManifest();
            String entry = manifest == null ? null : manifest.getMainAttributes().getValue("Agent-Class");
            modern = "local.flashback.modern.Agent".equals(entry)
                    || "local.flashback.modern.AgentV2".equals(entry);
        }
        String options = "state=" + state;
        if (modern) {
            rejectOptionDelimiter(agent, "Modern agent implementation path");
            options += ",implementation=" + agent;
        }
        return options;
    }

    private static void rejectOptionDelimiter(Path path, String label) {
        if (path.toString().contains(","))
            throw new IllegalArgumentException(label + " must not contain a comma; agent options are comma-separated.");
    }

    private static void usage() {
        System.err.println("Usage:");
        System.err.println("  install <PID> <runtime-recorder-agent.jar> <state-dir>");
        System.err.println("  command <state-dir> start|start:<20..120>|stop|mark:<name>|quit");
        System.err.println("  status <state-dir>");
    }
}
