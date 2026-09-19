# 第三方代码

核心模块固定在以下提交，不在运行时自动下载或更新：

* ACSM Input：<https://github.com/Leseratte10/acsm-calibre-plugin>
  提交 `f24cd01212df7c6d3a8e704b6ab875068e935aad`，GPL-3.0-or-later。
  保留 libadobe、libadobeAccount、libadobeFulfill、customRSA 及许可证。
  本地修改：用 cryptography 的 PKCS#12 接口代替 oscrypto；集成层替换网络传输，启用证书校验、HTTPS、超时；不输出上游可能包含授权数据的调试文本。
* DeDRM：<https://github.com/apprenticeharper/DeDRM_tools>
  提交 `776f146ca00d11b24575f4fd6e8202df30a2b7ea`，ineptepub 模块标注 GPL v3。
  本地修改：优先且仅使用 PyCryptodome，避免旧 OpenSSL ABI。

原版权注释保留在各文件中，GPL 全文位于 `src/playbooks/vendor/acsm/LICENSE`。
分发包含这些模块的组合版本时，须遵守适用 GPL 条款并提供相应源码及修改说明。
本集成目前仅处理 Adobe ADEPT EPUB；不支持 PDF、Kindle 或其他 DRM 方案。
