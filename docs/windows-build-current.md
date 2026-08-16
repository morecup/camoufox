# 当前 Windows 构建流程

本文档记录当前仓库在 `C:\orchids_recovered\camoufox` 上已经跑通的 Windows x86_64 构建、打包、固定目录发布和指纹回归验证流程。当前阶段只关注 Windows，Linux 构建先忽略。

## 当前基线

- 仓库路径：`C:\orchids_recovered\camoufox`
- 当前版本：`146.0.1-beta.25`
- Firefox 源码包：`firefox-146.0.1.source.tar.xz`
- Camoufox 源码树：`camoufox-146.0.1-beta.25`
- Windows objdir：`camoufox-146.0.1-beta.25\obj-x86_64-pc-mingw32`
- 源码树构建产物：`camoufox-146.0.1-beta.25\obj-x86_64-pc-mingw32\dist\bin\camoufox.exe`
- 根目录 zip 产物：`camoufox-146.0.1-beta.25-win.x86_64.zip`
- 稳定运行目录：`C:\orchids_recovered\camoufox\camoufox-builds\146.0.1-beta.25-win\camoufox.exe`
- 当前稳定 exe SHA-256：`5a6fb9b58ad19481025d71646f81ac9b628949d0db77c666d30e96d9cc55e1a6`

## 环境前提

当前机器已经具备以下依赖；新机器需要先补齐后再跑本文命令。

- Windows 10/11 x64。
- Visual Studio Build Tools / Mozilla build 依赖已经由 `mach bootstrap --application-choice=browser` 配好。
- Python venv：`.venv\Scripts\python.exe`。
- GNU Make、Git Bash 或等价 shell，可执行 `make` 和 `bash`。
- 7-Zip，可执行 `7z` 或位于 `C:\Program Files\7-Zip\7z.exe`。
- Node.js，用于 `build-tester` 首次安装和打包 TypeScript checks。

如果需要访问远端 git，统一走代理 `192.168.124.4:33210`。本地编译和本地验证不需要这个代理。

## 构建原则

- 只构建 Windows：`BUILD_TARGET=windows,x86_64`。
- 不要同时跑两条 `mach build` 或打包链路写同一个 `obj-x86_64-pc-mingw32`，并发会污染 objdir。
- 如果出现 `-juggler-pipe` / `-foreground` 不识别，优先怀疑补丁链或 objdir 污染，而不是简单补 zip 内容；必须重新 `make dir` 并单链路重建。
- 每次打包前应确认旧 zip 不被增量复用。当前 `scripts\package.py` 已改为删除旧包后全量 `7z a` 重建。
- `BUILD_TARGET` 写成精确的 `windows,x86_64`，不要带尾随空格。

## 一次完整重编

在 PowerShell 中从仓库根目录开始：

```powershell
cd C:\orchids_recovered\camoufox
$env:BUILD_TARGET = 'windows,x86_64'
```

准备或重置 Camoufox 源码树，并重新套完整 patch stack：

```powershell
make dir version=146.0.1 release=beta.25
```

进入源码树，单链路构建。当前稳定做法是直接调用 venv Python 跑 `mach`，并用 `-j1` 降低 Windows 上的并发不稳定性：

```powershell
cd C:\orchids_recovered\camoufox\camoufox-146.0.1-beta.25
..\.venv\Scripts\python.exe .\mach build -v -j1
```

构建成功后应存在：

```text
C:\orchids_recovered\camoufox\camoufox-146.0.1-beta.25\obj-x86_64-pc-mingw32\dist\bin\camoufox.exe
```

回到仓库根目录打包：

```powershell
cd C:\orchids_recovered\camoufox
make package-windows arch=x86_64 version=146.0.1 release=beta.25
```

打包成功后应存在：

```text
C:\orchids_recovered\camoufox\camoufox-146.0.1-beta.25-win.x86_64.zip
```

## 产物校验

先记录源码树 exe 和根 zip 的时间戳、大小、哈希：

```powershell
Get-Item .\camoufox-146.0.1-beta.25\obj-x86_64-pc-mingw32\dist\bin\camoufox.exe
Get-FileHash -Algorithm SHA256 .\camoufox-146.0.1-beta.25\obj-x86_64-pc-mingw32\dist\bin\camoufox.exe

Get-Item .\camoufox-146.0.1-beta.25-win.x86_64.zip
Get-FileHash -Algorithm SHA256 .\camoufox-146.0.1-beta.25-win.x86_64.zip
```

然后解压 zip，确认 zip 内 `camoufox.exe` 与源码树 `dist\bin\camoufox.exe` 哈希一致：

```powershell
$extractDir = 'C:\orchids_recovered\camoufox\tmp\win_pkg_extract_current'
if (Test-Path $extractDir) { Remove-Item -LiteralPath $extractDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
7z x .\camoufox-146.0.1-beta.25-win.x86_64.zip "-o$extractDir" -y
Get-FileHash -Algorithm SHA256 "$extractDir\camoufox.exe"
```

如果 zip 内 exe 哈希不等于源码树 exe 哈希，先停下来查打包流程，不要发布。

## 固定目录发布

外部项目不要继续依赖 `Users\Administrator\AppData\Local\camoufox\camoufox\Cache\...` 这种缓存路径。当前固定使用：

```text
C:\orchids_recovered\camoufox\camoufox-builds\146.0.1-beta.25-win\camoufox.exe
```

发布新构建时，将刚解压验证通过的整包目录复制到固定目录。复制前先备份旧目录：

```powershell
$stableDir = 'C:\orchids_recovered\camoufox\camoufox-builds\146.0.1-beta.25-win'
$backupDir = "${stableDir}.bak.$(Get-Date -Format yyyyMMddHHmmss)"
if (Test-Path $stableDir) { Rename-Item -LiteralPath $stableDir -NewName (Split-Path -Leaf $backupDir) }
Copy-Item -LiteralPath 'C:\orchids_recovered\camoufox\tmp\win_pkg_extract_current' -Destination $stableDir -Recurse
```

固定目录需要包含 `version.json`，当前内容口径如下：

```json
{
  "version": "146.0.1",
  "release": "beta.25",
  "target": "windows",
  "arch": "x86_64"
}
```

发布后再次确认固定目录 exe 哈希：

```powershell
Get-FileHash -Algorithm SHA256 C:\orchids_recovered\camoufox\camoufox-builds\146.0.1-beta.25-win\camoufox.exe
```

## build-tester 回归

`build-tester` 是 raw binary 级别验证，不走 Python package 下载路径。首次或依赖变化后安装依赖：

```powershell
cd C:\orchids_recovered\camoufox\build-tester
npm install
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

运行完整 8 profiles：

```powershell
cd C:\orchids_recovered\camoufox
$exe = 'C:\orchids_recovered\camoufox\camoufox-builds\146.0.1-beta.25-win\camoufox.exe'
$out = 'C:\orchids_recovered\camoufox\tmp\windows_build_current'
New-Item -ItemType Directory -Force -Path $out | Out-Null
.\.venv\Scripts\python.exe .\build-tester\scripts\run_tests.py $exe --profile-count 8 --save-cert "$out\build_tester_cert.txt"
```

通过标准：

- 进程退出码为 `0`。
- 总体结果为 `A`。
- 分数为 `1048/1048`。
- `errorCount=0`，`failedTests=[]`。
- 关键段落应通过：Automation、Workers、Headless Detection、Speech Voices、Font Environment、WebRTC、Canvas、WebGL、Audio、Stability。

如果偶发失败，先查是否有旧的 `camoufox.exe` headless / juggler 残留进程。当前 `build-tester\scripts\run_tests.py` 已带 Windows pre-run cleanup，只会清理同一 binary path 下的测试残留进程。

## 外站指纹回归

运行 Windows 口径的外站回归：

```powershell
cd C:\orchids_recovered\camoufox
$exe = 'C:\orchids_recovered\camoufox\camoufox-builds\146.0.1-beta.25-win\camoufox.exe'
$report = 'C:\orchids_recovered\camoufox\tmp\windows_build_current\fingerprint_suite_windows.json'
.\.venv\Scripts\python.exe .\scripts\fingerprint_suite_windows.py --executable-path $exe --output $report --pretty
```

必须覆盖并检查以下站点：

- BrowserLeaks JavaScript。
- BrowserLeaks Client Hints。
- BrowserLeaks Canvas。
- BrowserLeaks WebGL。
- BrowserLeaks Fonts。
- CreepJS。
- Sannysoft。

通过标准：

- JSON 中 BrowserLeaks、CreepJS、Sannysoft 全部 `status=ok`。
- `summary.highRiskFindings=[]`。
- UA 和 worker UA 均为 Firefox 146 形态。
- `navigator.webdriver=false`。
- Client Hints 为 Firefox 口径的 `Disabled / Not Supported` / `undefined`。
- timezone、locale、WebRTC IP 与网络画像一致。
- Canvas、WebGL、Audio、Fonts、Speech Voices 没有退回本机裸指纹或明显 headless/software renderer 信号。
- CreepJS `headless=0%`、`likeHeadless=0%`。
- Sannysoft 的 `WebDriver Advanced` 应为 passed。

当前已知低风险观察项：Sannysoft 可能仍显示 `Chrome (New)=missing (failed)` 和 `Permissions (New)=prompt`。这是 Firefox 口径常见告警，不单独判为 Windows 指纹回归阻塞。

## 直接启动 smoke

固定目录发布后，至少做一次直接启动探针，确认 binary 能被 Playwright 接管：

```powershell
cd C:\orchids_recovered\camoufox
$env:CAMOUFOX_EXECUTABLE_PATH = 'C:\orchids_recovered\camoufox\camoufox-builds\146.0.1-beta.25-win\camoufox.exe'
@'
from camoufox.sync_api import Camoufox

with Camoufox(headless=True, i_know_what_im_doing=True, os="windows") as browser:
    page = browser.new_page()
    page.goto("about:blank")
    print(page.evaluate("() => navigator.userAgent"))
    print(page.evaluate("() => navigator.webdriver"))
'@ | .\.venv\Scripts\python.exe -
```

期望：UA 为 Firefox/Camoufox 146 相关口径，`navigator.webdriver` 为 `False`。

## 常见故障和处理

### `-juggler-pipe` 不识别

这通常说明 Juggler/LW 没编进去，不能只往 `omni.ja` 里补文件。处理方式：停止旧构建链，重新跑 `make dir version=146.0.1 release=beta.25`，确认源码树重新回到 `unpatched` 后完整套 patch，再单链路 `mach build -v -j1`。

### build-tester 出现 Linux/macOS uniqueness 单点波动

先看 `failedTests`。如果 `1048/1048` 且 `failedTests=[]`，证书里的 uniqueness 统计波动一般不作为 Windows 二进制回归。若有真实失败，再看是否混入软件 WebGL preset、重复 screen preset 或 stale process。

### WebGL 出现 SwiftShader / llvmpipe / software rasterizer

这一般是 tester preset 或运行环境问题。当前 `build-tester\scripts\presets.py` 和 `generate-presets.py` 已过滤软件 WebGL preset；如果复现，先确认正在运行的是最新 tester 代码。

### 外站 timezone / WebRTC 与出口 IP 不一致

优先检查网络画像和代理设置。raw binary 探针如果没有带 network override，可能显示本机 timezone 或本机 WebRTC，不应直接等同 wrapper 路径回归。最终判断以 `build-tester` 和 `fingerprint_suite_windows.py` 的 wrapper/正式路径为准。

## 发布给 orchids-account-manager

`C:\orchids_recovered\rewritten-minimal-set\orchids-account-manager` 当前应使用固定目录：

```text
C:\orchids_recovered\camoufox\camoufox-builds\146.0.1-beta.25-win\camoufox.exe
```

修改 `orchids-account-manager.json` 前必须先备份配置。修改后确认 JSON 可解析，并确认 sidecar resolver 能得到：

```text
executable_path=C:\orchids_recovered\camoufox\camoufox-builds\146.0.1-beta.25-win\camoufox.exe
ff_version=146
```

如果 `orchids-account-manager` 已经启动过 Camoufox sidecar，替换 binary 或 sidecar 代码后需要关闭旧 sidecar 或重启主服务，否则可能继续复用旧进程。

## 每轮记录项

每次 Windows 重编和回归后，至少记录以下内容：

- 当前阶段。
- 源码树 exe 路径、时间戳、大小、SHA-256。
- zip 路径、时间戳、大小、SHA-256。
- 固定目录 exe 路径和 SHA-256。
- `build-tester` 摘要：grade、score、exitCode、errorCount、failedTests。
- `fingerprint_suite_windows.py` 摘要：BrowserLeaks、CreepJS、Sannysoft 状态和 high-risk findings。
- 已确认问题、首个真实阻塞、是否回归。
- 本地关键路径和日志路径。
