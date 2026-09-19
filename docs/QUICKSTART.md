# 使用说明

Miniconda 安装及启动见 [README](../README.md)。以下命令均在项目目录、执行 `conda activate epub` 后运行。

## GUI 流程

1. **ADE 授权**：优先导入已有授权目录；没有授权时再登录，避免重复占用设备额度。账户迁移说明见 ID 旁的帮助按钮。密码不会保存。
2. **文件与处理**：添加 ACSM 或 EPUB，选择处理模式。卷 ID 通常可自动识别；有歧义时手动填写阅读器地址 `reader?id=...` 中的 ID。
3. 首次抓图勾选「显示浏览器」并登录拥有该书的 Google 账户；不要同时让其他任务使用同一浏览器配置目录。
4. 点击开始。取消会保留已下载缓存，并等待当前请求及浏览器清理，可能不会立即结束。

仅准备 EPUB 不需要 Google 登录；本地替换需要选择已下载的原图目录。界面语言与主题切换不会修改书名或书籍内容。

## 常用命令

```sh
python playbooks_app.py import-auth --folder "已有授权目录"
python playbooks_app.py authorize
python playbooks_app.py process --input "书籍.acsm"
python playbooks_app.py process --input "书籍.epub" --images "playbooks_work/卷ID"
python playbooks_app.py process --input "书籍.acsm" --output-dir "输出目录"
python playbooks_app.py process --input "书籍.epub" --dry-run
```

`--dry-run` 不发布 EPUB，但可能进行准备和下载、写入缓存。统一入口需显式添加 `--overwrite` 才能覆盖已有输出。

抓图 CLI 也可分步使用：

```sh
python playbooks_hires.py fetch --id "卷ID" --show-browser
python playbooks_hires.py replace --epub "书籍.epub" --images "playbooks_work/卷ID"
python playbooks_hires.py run --epub "书籍.epub" --id "卷ID"
```

抓图 CLI 的 `replace` / `run` 保留原有覆盖行为，目标文件存在时会被替换；需要覆盖保护时使用统一入口。源 EPUB 不会被覆盖，生成文件校验通过后才发布。

## 路径与缓存

`--state`、`--profile`、`--work` 分别设置授权、浏览器配置和原图缓存目录；`--out` 指定完整输出文件名。默认相对项目根目录，显式传入的相对路径按 CLI 当前工作目录解释。

「清除本地图源缓存」只将识别出的原图缓存移至系统回收站，不删除 ADE 授权、浏览器登录或已输出书籍。不明目录及包含链接的缓存会跳过。清理前请停止其他实例的任务。

## Windows 注意事项

- 使用 Miniconda 的 Anaconda Prompt，执行 `conda activate epub` 后运行 GUI 或 CLI；环境采用 Python 3.12 x64。
- 激活环境后可运行 `pythonw playbooks_gui.py` 启动 GUI，不创建 Python 控制台窗口；排查启动错误时使用 `python playbooks_gui.py`。
- 自动查找常见位置的 Chrome、Edge 和 Brave；自定义安装可在 CLI 传 `--browser "浏览器完整路径"`。
- 路径含空格时加引号。使用短目录，避免 Windows 总路径长度限制；文件名中的保留字符及设备名称会处理。
- 不要跨设备复制正在使用的 Chrome 配置；在 Windows 上重新登录 Google。ADE 授权通过导入功能处理。
- 授权和缓存继承 Windows 目录权限，`.gitignore` 不提供加密或访问保护。

## 排查

- 先运行 `python playbooks_app.py status` 检查依赖和浏览器。
- 找不到书：填写正确卷 ID，并确认登录账户确实拥有该书。
- 插图缺失：显示浏览器确认可读范围；预览及出版源本身缺失的图片无法保证补齐。
- 授权占用：等待其他处理任务退出，不要手动删除正在使用的锁文件。
- 取消稍慢：网络请求有超时，清理完成前不要强制关闭窗口。

目前仅支持 Adobe ADEPT EPUB，不支持 PDF、Kindle 或其他 DRM 格式。
