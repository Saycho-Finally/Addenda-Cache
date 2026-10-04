# 手动推送步骤（GitHub 网络通时执行）

前置：代理/VPN 已开启（能访问 github.com），GitHub 已登录过（Addenda-LM 推送过即满足）。

## 方式一：一键脚本（推荐）

双击 `push.bat`。它自动完成 add → commit → push，并在失败时给出排查提示。

## 方式二：命令行依次执行

在本目录（仓库2 根目录）打开终端（cmd 或 PowerShell），依次执行：

```bat
cd /d "C:\Users\30312\Desktop\仓库2"

:: 1. 暂存全部变更
git add -A

:: 2. 提交（首次已有初始 commit，此步仅在有新变更时需要）
git commit -m "update: content sync"

:: 3. 推送（-c 参数跳过 Windows 证书吊销检查——某些代理环境下必需）
git -c http.schannelCheckRevoke=false push -u origin main
```

第 3 步首次推送时如果弹出浏览器/窗口要求登录 GitHub，完成登录即可（Git Credential Manager 会记住）。

## 排错速查

| 症状 | 处置 |
|---|---|
| `schannel: ... CRYPT_E_NO_REVOCATION_CHECK` | 命令里已带 `-c http.schannelCheckRevoke=false`；仍报错则确认代理已开 |
| `SSL certificate ... unable to get local issuer certificate` | 换命令：`git -c http.sslBackend=openssl -c http.sslCAInfo="C:\Program Files\Git\mingw64\etc\ssl\certs\ca-bundle.crt" -c http.sslVerify=false push -u origin main` |
| `403` 或要求重复登录 | GitHub 凭据过期 → 控制面板 → 凭据管理器 → Windows 凭据 → 删除 `git:https://github.com`，重推时重新登录 |
| `Everything up-to-date` | 已经推过了，无需操作 |

## 验证

推送成功后刷新 https://github.com/Saycho-Finally/Addenda-Cache ，应看到 README、cachecortex/、benchmarks/、reports/、results/ 等目录。
