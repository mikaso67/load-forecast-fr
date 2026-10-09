# Compile a test-only binary under Windows PowerShell 5.1; no real Docker calls.
param([Parameter(Mandatory = $true)][string]$OutputPath)
$ErrorActionPreference = 'Stop'
$LffCode = @'
using System;
using System.IO;
public class FakeDocker {
    public static void Main(string[] args) {
        File.AppendAllText(Environment.GetEnvironmentVariable("LFF_TEST_DOCKER_LOG"),
                           String.Join("|", args) + "\n");
        string database = args[args.Length - 1];
        if (Environment.GetEnvironmentVariable("LFF_TEST_BACKUP_FAIL_DB") == database) {
            Console.Error.Write(new string('x', 100000));
            Console.Error.Write("FAKE_PRIVATE_ERROR_MUST_NOT_APPEAR");
            Environment.Exit(2);
        }
        Stream output = Console.OpenStandardOutput();
        byte[] payload = new byte[] {80, 71, 68, 77, 80, 0, 10, 13, 128, 255, 1, 2, 3};
        if (Environment.GetEnvironmentVariable("LFF_TEST_BACKUP_INVALID") == "1") {
            payload[0] = 0;
        }
        output.Write(payload, 0, payload.Length);
        output.Flush();
    }
}
'@
Add-Type -TypeDefinition $LffCode -OutputAssembly $OutputPath -OutputType ConsoleApplication
