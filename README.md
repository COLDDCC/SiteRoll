# SiteRoll

把多个网站的配置和 Google 收录记录放进一张中文总表，每周自动检查，只告诉你哪里变了、哪里需要处理。免费开源、本地运行，Python 标准库实现，不调用 AI API，不需要域名、服务器或第三方 Python 包。

每周生成的周报同时有中文 Markdown（给人看）和结构化 JSON（给 AI 分析）两份，方便后续交给 AI 整理成审核单。

## 启动

需要 Python 3.10 或更新版本。下载仓库 ZIP 并解压，进入目录：

```bash
python app.py
```

Windows 也可双击 `start.bat`（需预先安装 Python 并启用 Python launcher）。浏览器会打开 http://127.0.0.1:8765 。关闭终端或按 Ctrl+C 停止。端口已被使用时，请关闭之前的 SiteRoll 实例。

### 不连接 Google 也能用

1. 添加网站，例如 `https://superpokemonteambuilder.com/`。
2. 留空 sitemap 默认读取 `/sitemap.xml`，也可填写子站点地图地址。
3. 点击“配置”或“检查全部 · 网站配置”。
4. 在历史记录下拉框打开本次结果，筛选需要关注的页面、导出 CSV。

检查 HTTP 状态、重定向链、HTML robots/googlebot、X-Robots-Tag、canonical 和标题。**不检查 robots.txt 阻止规则，不渲染 JavaScript，技术检查不能证明 Google 已收录。** HTTP/WWW 跳转、备用版本和反馈页 noindex 不一定需要修改。

## 连接 Google：两种方式

| 方式 | 适合 | 说明 |
|---|---|---|
| **服务账号（推荐用于定时检查）** | 无人值守、长期运行 | 不会过期，无需浏览器授权。需要把服务账号邮箱加为每个 GSC 属性的用户。 |
| OAuth 桌面客户端 | 偶尔手动查看 | 用自己的 Google 账号授权；测试模式下令牌约 7 天过期，需要重新连接。 |

两者都存在时优先使用服务账号。

### 服务账号（推荐）

1. 打开 https://console.cloud.google.com/ ，创建项目，在 API 库启用 **Google Search Console API**。
2. 打开 IAM 和管理 → 服务账号，创建服务账号（不需要授予任何项目角色）。
3. 进入该服务账号 → 密钥 → 添加密钥 → JSON，下载文件。
4. 将文件重命名为 `service-account.json` 放在 `app.py` 旁边（已被 .gitignore 忽略），或设置环境变量 `SITEROLL_SERVICE_ACCOUNT` 为文件路径或 JSON 内容。
5. 在 Search Console 每个属性的 设置 → 用户和权限 中，把服务账号邮箱（形如 `xxx@项目.iam.gserviceaccount.com`）添加为用户，权限选“受限”即可。
6. 启动 SiteRoll，顶部显示“服务账号”即已生效；“从 Google 导入”会列出服务账号能访问的属性。

签名使用 Python 标准库实现的 RS256，无需安装 `cryptography` 等第三方包。

## 首次连接 Google：OAuth（只读）

每位使用者使用自己的 Google Cloud 项目和 OAuth 客户端；此仓库没有共享客户端，也不会索取 Google 密码。

1. 打开 https://console.cloud.google.com/ ，创建项目（例如 SiteRoll）。
2. 在 API 库启用 **Google Search Console API**。
3. 打开 Google Auth Platform，配置应用名称、联系邮箱和 Audience。个人 Gmail 通常选择 External；测试模式下把自己的 Google 账号加入 Test users。
4. 在 Data Access 添加只读 scope：`https://www.googleapis.com/auth/webmasters.readonly`。
5. 在 Clients 创建 **Desktop app / 桌面应用** 类型的 OAuth 客户端，下载 JSON。
6. 将 JSON 重命名为 `credentials.json`，放在 `app.py` 旁边。不要上传 GitHub。
7. 启动 SiteRoll，点击“连接 Google”，在 Google 官方页面授权。回调自动返回本机，授权页提示成功后返回工具。
8. 点击“从 Google 导入”，选择你有权限的网站。
9. 检查网站的 sitemap 是否正确，点击“收录”或“检查全部 · Google 收录”。

测试模式的授权可能需要周期性重新连接（Google 对部分测试应用的刷新令牌有七天期限）。如果看到“未验证应用”，确认这是你自己的项目和客户端；对外发行共用 OAuth 客户端时，需要按 Google 的验证规则单独准备，不能将本项目当作已经通过验证的在线服务。

域名属性形如 `sc-domain:example.com`；URL 前缀属性形如 `https://example.com/`，必须与 GSC 一致。同一网站重新保存可更改属性和 sitemap。域名属性默认导入 HTTPS 根地址，子目录属性默认导入该前缀下的 sitemap；请按实际网站调整。

## 自动定时检查

```bash
python app.py run                    # 配置检查 + 增量收录检查 + 生成周报
python app.py run --mode gsc         # 只查收录
python app.py run --full             # 忽略增量规则，全部重新查询（仍受配额限制）
python app.py run --site example.com # 只查某个网站，可重复
python app.py report                 # 不检查，只根据已有记录生成周报
python app.py add-site https://example.com/ --property sc-domain:example.com
python app.py sites                  # 列出网站
```

周报写入 `data/reports/`：`weekly-日期.md/.json` 和始终指向最新一份的 `latest.md/.json`。

**Windows 任务计划程序**（每周一 9:00，路径改成你的）：

```bat
schtasks /create /tn "SiteRoll 每周检查" /sc weekly /d MON /st 09:00 /tr "\"C:\SiteRoll\run-weekly.bat\""
```

`run-weekly.bat` 会运行检查并把输出追加到 `data/run.log`。电脑需要在该时间开机；任务计划程序可勾选“错过后尽快运行”。

**macOS / Linux cron**：

```cron
0 9 * * 1 cd /path/to/SiteRoll && python3 app.py run >> data/run.log 2>&1
```

## 增量检查规则

Google 每个属性每天只允许约 2000 次 URL Inspection，所以默认不是每次全量查询，而是按价值排序：

1. sitemap 中的新页面（从未查过）
2. 上次查询失败或状态未知
3. 未收录页面，距上次查询超过 3 天
4. sitemap `lastmod` 晚于上次查询的页面
5. 已收录页面，距上次查询超过 14 天

其余“近期已查过”的页面本次跳过。每站每次最多查询 200 个（`--budget` 可改，`0` 为不限），并且不超过该属性当日剩余的安全配额。URL 前缀属性只查询属于该前缀的网址，其余计为“不属于该属性”。

## 周报怎么读

每个网址按最近一次 Google 记录归入一类，周报按网站汇总：

| 分类 | 含义 | 建议 |
|---|---|---|
| 可能出错 | robots.txt 阻止、sitemap 中的页面却设置了 noindex、404、软 404、5xx 等 | 多半是网站配置问题，优先处理 |
| 需要人工判断 | Google 选择了与页面声明不同的规范网址 | 看是否真的重复 |
| 观察 | 已发现/已抓取但尚未编入索引 | 多与内容质量、内链、时间有关，不建议批量改动 |
| 通常正常 | 页面主动把规范网址指向别处 | 一般无需处理 |
| 失败 / 未知 | 查询出错或 Google 未给结论 | 不等于未收录 |

“需要关注”的网站排在最前，原因包括：失去收录、可能出错的网址、检查出错、本周没有新的收录数据、未设置 GSC 属性。JSON 中每类附带网址示例、sitemap 新增/移除的页面、最近一次配置检查的提示，便于 AI 进一步分析。

## 实现的功能与边界

- 多网站增删改；无 50 个站的硬编码限制。
- XML sitemap / sitemap index / gzip；www 与非 www 视为同一网站，域名属性包含子域名。收录检查最多读取 5 万个网址、100 个 sitemap 文件；配置检查需要逐页抓取，每站每次最多 500 页。超限会明确报错，不假装全站已检查。
- Google URL Inspection 官方索引记录：verdict、coverageState、上次抓取、声明和 Google 选择的 canonical。
- 每次检查保存历史，显示完整/中断状态，CSV 导出。支持同类检查快照对比：新增收录、失去收录、首次检查、新配置提示和提示消失。未知、失败、缺失结果不算失去收录。检查结束自动打开本批第一个网站的结果，其余可从历史中选择。
- 后台串行任务与进度。单个网址遇到 429/5xx/网络错误会退避重试，仍失败则记为该网址失败并继续；授权、权限、配额错误或连续 5 次失败才停止该站并继续其他站。失败不会被标成“未收录”，已完成的结果保留。
- 限速、超时、每属性每日安全上限 1500 次（本地按 UTC 计数）；Google 官方上限 2000 次/日、600 次/分钟。其他工具可能消耗同一属性配额。
- 手动检查或通过 `python app.py run` 定时检查（借助任务计划程序 / cron，电脑关机时不会运行）。暂无消息推送，周报以文件形式保存。
- 不支持“测试实际网址”，不能请求索引，也不保证收录或排名。

## 数据与隐私

`data/siteroll.db` 保存网站、sitemap 页面、结果与调用计数，`data/token.json` 保存 OAuth 授权令牌，`data/reports/` 保存周报。服务账号密钥 `service-account.json` 等同于账号密码，不要分享或上传。令牌不返回给前端；在支持 POSIX 的系统上以 0600 权限保存。文件没有额外加密，在 Windows 上依赖你的用户文件权限；请保护电脑账户和磁盘。断开本地连接删除本地令牌，但不会撤销 Google 端授权，后者请在 Google 账号第三方连接页面撤销。

工具直接访问添加的网站及 Google API。网页不接入广告、分析统计或外部前端资源。只绑定 `127.0.0.1`，检查 Host、会话请求令牌及 Origin。拒绝私有/本机地址，并对每个跳转重新校验 DNS；**不应部署为公网抓取服务**，当前 DNS 校验没有固定解析 IP，公网多用户部署需要额外的 DNS 重绑定防护、网络隔离、账户系统与凭证加密。

本地 `data/`、`credentials.json`、`service-account.json` 已忽略。分享源码时不要分享这些文件。复制 `data/` 可备份自己的历史（同时包含授权凭证，须安全保存）。

## 开发验证

```bash
python -m unittest discover -s tests -v
python -m py_compile app.py
```

测试使用模拟响应，不需要 Google 凭证。真实 OAuth 和真实收录查询需要用户配置凭证后验证。

官方参考：
- https://developers.google.com/webmaster-tools/v1/urlInspection.index/inspect
- https://developers.google.com/webmaster-tools/v1/sites/list
- https://developers.google.com/webmaster-tools/limits
- https://developers.google.com/identity/protocols/oauth2/native-app

## 后续计划

1. 用真实 GSC 数据验证一个月的周报。
2. AI 读取 `latest.json`，生成中文审核单（哪些站要处理、为什么、建议动作）。
3. 对确认的技术问题，AI 在网站仓库提交修复 PR，人工审核后合并、自动部署。
4. 可选：在 GitHub Actions 中定时运行（服务账号授权，电脑关机也能运行）。

MIT License.

## 收录统计与变化报告

每站统计使用最近一次 GSC 检查，不混入配置检查。发现数来自当次成功读取的 sitemap；成功查询、失败、未检查分别显示，未知状态不计为未收录。旧版本的失败记录可能没有发现数，显示“未知”。新检查中断时仍保留发现数与部分结果。

对比上次同站、同类检查，仅比较两次都有明确结果的相同网址。首次检查建立基线；本次新增的网址表示“首次检查”，不是刚刚被 Google 收录。某网址不在本次结果中不会被判断为失去收录。Google 状态变化也不代表工具确定了原因。
