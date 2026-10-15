# 使用说明

uv 安装见 [README](../README.md)。以下命令均在项目目录通过 `uv run` 运行。

## GUI 流程

1. **ADE 授权**：处理 ACSM 或 Adobe 加密 EPUB 时，优先导入已有授权；没有授权时使用 ByteBooks ID 登录。新授权会注册设备，已有授权可用时无需重新登录。账户迁移说明见 ID 旁的帮助按钮，密码不会保存。
2. **Play 书库**：点击「登录 Google」，在专用浏览器中手动登录拥有该书的账户。验证成功后自动加载书库；选书并点击「添加所选图书」，程序会尝试通过官方菜单导出 ACSM / EPUB，无法导出时需关联本地文件。
3. **任务处理**：也可点击「添加电子书」选择本地 ACSM / EPUB，再选择「任务类型」。从书库加入的任务会带入卷 ID；本地文件会尝试自动识别，无法确定时可查看日志中的候选，或填写阅读器地址 `reader?id=...` 中的 ID。
4. **设置**：按需修改输出目录、本地原图目录、浏览器配置、抓图缓存及匹配参数。「显示抓图浏览器」只控制抓图窗口是否可见，不是登录入口。
5. 在「任务处理」点击「开始处理」。取消会保留已下载缓存，并等待当前请求及浏览器清理，可能不会立即结束。

普通无 DRM EPUB 不需要 ADE 授权。「只获取 EPUB：兑换 / 解密」不需要 Google 登录；「只用本地原图替换」需在设置中选择已有原图目录，不会联网抓图。

## Google 登录

除 GUI 外，也可通过命令行登录：

```sh
uv run python playbooks_app.py login-google
```

使用自定义配置时，登录和处理都传入相同的 `--profile "目录"`，并使用同一浏览器（CLI 可指定 `--browser "可执行文件"`）。不要选择日常 Chrome 的用户数据目录，也不要跨平台复制用户数据目录。

登录窗口使用普通浏览器模式，不启用远程调试或无头模式。登录检测只读取 Cookie 名称和时间戳，不读取 Cookie 值或密码。检测到新的登录标记后，会尝试关闭本次打开的专用浏览器，再后台刷新书库验证；读取到图书才完成登录，书库为空或验证失败时会重新打开登录窗口。日常浏览器不受影响。

自动检测或关闭失败时，可手动关闭专用浏览器继续。取消只停止等待，登录浏览器需手动关闭。登录和抓图不能同时使用同一个配置目录；若提示已有调试浏览器，请先结束旧任务并关闭该浏览器。

若登录仍提示“不安全”，请先检查 Chrome 更新，以及同一设备、网络下普通浏览器能否登录；不要关闭安全检查。

## 常用命令

```sh
uv run python playbooks_app.py import-auth --folder "已有授权目录"
uv run python playbooks_app.py authorize
uv run python playbooks_app.py process --input "书籍.acsm"
uv run python playbooks_app.py process --input "书籍.acsm" --prepare-only
uv run python playbooks_app.py process --input "书籍.epub" --images "playbooks_work/卷ID"
uv run python playbooks_app.py process --input "书籍.acsm" --output-dir "输出目录"
uv run python playbooks_app.py process --input "书籍.epub" --dry-run
```

`--prepare-only` 只获取可读 EPUB，不抓图或替换插图。`--dry-run` 不生成输出 EPUB，但可能下载书籍和原图、写入缓存。

`playbooks_app.py process` 默认不覆盖同名输出文件；如需覆盖，添加 `--overwrite`，GUI 中对应设置里的「允许覆盖已有输出」。

也可分步抓图和替换；替换时需使用无 DRM 的 EPUB：

```sh
uv run python playbooks_hires.py fetch --id "卷ID"
uv run python playbooks_hires.py replace --epub "书籍.epub" --images "playbooks_work/卷ID"
uv run python playbooks_hires.py run --epub "书籍.epub" --id "卷ID"
```

`playbooks_hires.py replace` / `run` 会覆盖同名输出文件；需要覆盖保护时使用 `playbooks_app.py process`。源 EPUB 不会被覆盖，生成文件校验通过后才写入目标路径。

## 路径与缓存

未指定输出目录时，本地文件默认输出到源文件旁的 `hires/`；GUI 从 Play 书库加入的任务默认输出到项目下的 `hires/`。文件名优先采用原图元数据中的 Play 图书书名，再回退到 EPUB 书名、源文件名。

`--state`、`--profile`、`--work` 分别设置授权与工作缓存、浏览器配置和抓图缓存目录；`--output-dir` 设置输出目录，`--out` 指定完整输出文件名，两者不可同时使用。显式传入的相对路径按 CLI 当前工作目录解释。

「设置」中的「清除缓存」将可识别的原图、匹配索引及下载／准备的电子书缓存移至系统回收站，可恢复。保留 ADE 授权、浏览器登录、本地原图、当前队列源文件和输出 EPUB；不明目录及包含链接的缓存会跳过。清理前请停止其他窗口或 CLI 中的任务，再次使用可能需要重新下载。

## Windows 注意事项

- 使用 uv 管理环境（首次 `uv sync`，之后用 `uv run` 执行 GUI 或 CLI）；环境采用 Python 3.12 x64。
- 使用 `uv run pythonw playbooks_gui.py` 启动 GUI，不创建 Python 控制台窗口；排查启动错误时使用 `uv run python playbooks_gui.py`。
- 自动查找常见位置的 Chrome、Edge 和 Brave；自定义安装可在 CLI 传 `--browser "浏览器完整路径"`。
- 路径含空格时加引号。使用短目录，避免 Windows 总路径长度限制；文件名中的保留字符及设备名称会处理。
- 不要跨设备复制正在使用的 Chrome 配置；在 Windows 上重新登录 Google。ADE 授权通过导入功能处理。
- 授权和缓存继承 Windows 目录权限，`.gitignore` 不提供加密或访问保护。

## 排查

- 先运行 `uv run python playbooks_app.py status` 检查依赖和浏览器。
- 找不到书：优先从 Play 书库选书加入任务，或填写正确卷 ID，并确认登录账户确实拥有该书。
- 插图缺失：显示浏览器确认可读范围；预览及出版源本身缺失的图片无法保证补齐。
- 授权目录被占用：等待其他处理任务退出，不要手动删除正在使用的锁文件。
- 取消稍慢：网络请求有超时，清理完成前不要强制关闭窗口。

支持普通 EPUB，以及通过 ACSM 获取或使用 Adobe ADEPT 加密的 EPUB；不支持 PDF、Kindle 及其他 DRM 格式。
