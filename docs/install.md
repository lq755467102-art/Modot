# Install or build Modot Preview

## Windows x64

[Download Preview .15](https://github.com/lq755467102-art/Modot/releases/download/v0.2.0-preview.15/Modot-Preview-0.2.0-preview.15-x64-Setup.exe) · [SHA256SUMS.txt](https://github.com/lq755467102-art/Modot/releases/download/v0.2.0-preview.15/SHA256SUMS.txt) · [Source and license attachments](https://github.com/lq755467102-art/Modot/releases/tag/v0.2.0-preview.15)

Download `Modot-Preview-<version>-x64-Setup.exe` and `SHA256SUMS.txt` from the same release. The corresponding video source and notice archive is supplied alongside the installer. The installer is per-user. Close Modot Preview before running setup to upgrade it in place and keep your projects, conversations and settings. Preview has its own application identity and can coexist with the Pilot edition. You supply the model endpoint, model name and API key in **Settings → Models**. Start a chat or open a folder as a project. A normal folder works without Git.

Project commands and isolated project tests on Windows use the Dokan 2 filesystem driver in all three chat permission modes. The published .15 installer uses a separately installed Dokan 2.3.1.1000 x64 driver. Starting with the .16 candidate, interactive setup offers to install the bundled driver with Windows administrator approval. Silent setup leaves the driver choice for later. If needed, expand the failed project command in the conversation and click **Install or check the Dokan 2 driver** in the log. Modot can start, open a project and read its files before the driver is installed. Running a project command with the DLL missing reports that Dokan 2 must be installed and started. [Official Dokan Windows release](https://github.com/dokan-dev/dokany/releases/tag/v2.3.1.1000)

The Windows x64 .15 installer is **unsigned**. For a public signed build, compare the publisher in the Windows signature properties with the publisher announced for the release. The installer and installed application should both have a valid signature and a timestamp. A SHA-256 digest checks the download against the listed bytes. An attacker with access to the download page could replace both a file and an unsigned digest.

```powershell
$setup = Get-Item .\Modot-Preview-0.2.0-preview.15-x64-Setup.exe
Get-FileHash -Algorithm SHA256 -LiteralPath $setup.FullName
Get-AuthenticodeSignature -LiteralPath $setup.FullName |
    Select-Object Status, SignerCertificate, TimeStamperCertificate
```

The published .15 installer retrieves MinGit from Git for Windows and can use an offline cache. Starting with the .16 candidate, both MinGit and the Dokan installer are in the Windows installer. Setup can run offline on a machine without Git or a download cache. [Windows components and source inventory](../desktop/installer/WINDOWS-COMPONENTS.md) · [Video runtime and source pins](../desktop/installer/video-runtime.json) · [Third-party notices](../desktop/THIRD_PARTY.md)

The application archive includes this version's Modot license, its Chinese translation and the historical MIT notice. Keep those files with redistributed copies. The matching video source and notice archive accompanies the installer for the independent licenses of its third-party libraries. [Modot terms](../LICENSE) · [Chinese translation](../LICENSE.zh-CN.md) · [Historical MIT notice](licenses/previous-modot-mit.txt)

## Run from source

On Windows x64, use a normal CPython 3.13 installation, Node.js 24 and npm. The release checkout includes two npm lockfiles. From the repository root in PowerShell run the following commands.

```powershell
py -3.13 -m venv .venv
$env:HARNESS_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $env:HARNESS_PYTHON -m pip install -r requirements-workbench.txt
npm.cmd --prefix desktop ci
npm.cmd --prefix desktop/renderer-v2 ci
$env:HARNESS_SANDBOX_PYTHON_ROOT = (node desktop/scripts/prepare-python.cjs --sandbox-runtime)
& .\Modot-Preview.bat
```

The sandbox command stages a dedicated interpreter with the locked document, video and tool dependencies and prints its path. Keep that environment variable set in the shell that launches the preview or runs the tests. A virtual environment alone does not contain the standalone Windows runtime needed by isolated subprocesses.

If your npm configuration skips install scripts or the Electron binary download, run `npm.cmd --prefix desktop exec -- install-electron` before opening the preview. A source preview has a separate user-data directory. Set `MODOT_PREVIEW_HOME` to launch it in a fresh one. The initial UI is available in 中文 or English.

## Smoke tests and packaging

```powershell
& $env:HARNESS_PYTHON -m unittest tools.test_public_preview tools.test_video_attachments tools.test_documents
& $env:HARNESS_PYTHON tools/generate_workbench_protocol.py --check
& $env:HARNESS_PYTHON tools/scorecard.py
$env:MODOT_RELEASE_ASSETS = '<directory containing the video wheel and source archive>'
$env:MODOT_WINDOWS_COMPONENTS = '<build-inputs directory for Git and Dokan>'
& $env:HARNESS_PYTHON desktop/scripts/fetch-windows-components.py $env:MODOT_WINDOWS_COMPONENTS
npm.cmd --prefix desktop run dist:local
```

Use both input filenames and SHA-256 digests from [the video runtime lock](../desktop/installer/video-runtime.json). The package build verifies them, stages a fresh CPython runtime with the wheel and notices, and copies the source archive to the output directory as `Modot-<version>-Video-Sources-and-Notices.zip`. Publish it alongside the installer for that version. The build prints the output path at completion. For a signed release, use a Windows code-signing certificate with a private key accessible to the signing account. Keep the key with the signing provider or protected certificate store. Its SHA-1 thumbprint selects the certificate in Windows. Authenticode uses SHA-256 on the actual files. The build requests an RFC 3161 timestamp, verifies the signer of the app and installer, and fails if either verification fails.

```powershell
$env:MODOT_SIGNING_THUMBPRINT = '<40 hexadecimal characters from the Windows certificate>'
npm.cmd --prefix desktop run dist
```

For a release handoff, produce the SHA-256 manifest **after signing**, verify it against the downloadable files, and check the signature on a clean Windows machine. Each signature identifies the publisher of those bytes. SmartScreen may still show a reputation prompt. Certificate-backed signing and reputation are separate Windows checks. [Microsoft signing guidance](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options) · [Authenticode timestamps](https://learn.microsoft.com/en-us/windows/win32/seccrypto/time-stamping-authenticode-signatures)
