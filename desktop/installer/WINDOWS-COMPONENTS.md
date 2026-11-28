# Windows components

Modot Preview for Windows x64 bundles the original MinGit 2.56.1.windows.1 file tree and the original, signed Dokan 2.3.1.1000 x64 MSI. Git is local to Modot. Dokan installs its driver for the Windows system when the user approves. Windows Installer handles elevation. Uninstalling Modot leaves the shared driver available to other applications.

Find Git's notices in `resources/app.asar.unpacked/git/LICENSE.txt` and the component license directories in that tree. Dokan's library and driver use LGPL-3.0, its utilities and samples use MIT. The accompanying GPL and LGPL texts and notices are under `resources/app.asar.unpacked/dokan/licenses`. Third-party terms apply independently of the Modot license.

Distribute `Modot-<version>-Windows-Component-Sources.zip` alongside the installer. It contains the Dokany source tree, its pinned spdlog submodule, Git for Windows' packaging sources, and the source packages, patches and build recipes listed in `mingit-sources.json`. That index maps each entry in the shipped `git/etc/package-versions.txt` to a source package and SHA-256. The original packages can be opened with bsdtar. To rebuild Dokan, unpack its repository, place spdlog at `samples/dokan_memfs/spdlog`, and follow the repository's Windows build and installer instructions. For MinGit, follow the Git for Windows build-extra scripts and use the listed package versions and their PKGBUILDs. [Dokan upstream](https://github.com/dokan-dev/dokany/tree/f1d5de68ff459af94e309cfdd171e4b8ca2af4dd) · [Git for Windows build-extra](https://github.com/git-for-windows/build-extra/tree/d3fe43932de160518d345b93495ffbfc677f96a7)

For a Windows release build, run `py -3.13 desktop/scripts/fetch-windows-components.py <build-inputs-directory>` from the repository root, then set `MODOT_WINDOWS_COMPONENTS` to that directory. The fetcher verifies all pinned SHA-256 digests. A release build can use a previously verified inputs directory offline. The installed app and its installer do not run the fetcher.

Windows x64 安装包内含 MinGit 和 Dokan 官方驱动安装程序。MinGit 解压到 Modot 自己的目录。项目命令使用系统级 Dokan 驱动，由用户在安装时确认，也可稍后从会话的失败命令日志补装。组件许可证随程序安装，对应源码作为独立附件与同版安装包一起提供。
