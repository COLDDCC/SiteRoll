# SiteRoll

把多个网站的配置和 Google 收录记录放进一张中文总表。免费开源、本地运行，Python 标准库实现，不调用 AI API，不需要域名、服务器或第三方 Python 包。

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

## 首次连接 Google（只读）

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

## 实现的功能与边界

- 多网站增删改；无 50 个站的硬编码限制。
- 同域 XML sitemap / sitemap index；每站每次最多 500 页、最多 30 个 sitemap 文件。超限会明确报错，不假装全站已检查。不支持 gzip sitemap。
- Google URL Inspection 官方索引记录：verdict、coverageState、上次抓取、声明和 Google 选择的 canonical。
- 每次检查保存历史，显示完整/中断状态，CSV 导出。支持同类检查快照对比：新增收录、失去收录、首次检查、新配置提示和提示消失。未知、失败、缺失结果不算失去收录。检查结束自动打开本批第一个网站的结果，其余可从历史中选择。
- 后台串行任务与进度；GSC 出错会停止该站并继续其他站，避免失败被标成“未收录”。失败前已完成的结果保留。
- 限速、超时、每属性每日安全上限 1500 次（本地按 UTC 计数）；Google 官方上限 2000 次/日、600 次/分钟。其他工具可能消耗同一属性配额。
- 手动批量检查；**此版本没有自动每周调度或通知**。电脑关机时不会运行。
- 不支持“测试实际网址”，不能请求索引，也不保证收录或排名。

## 数据与隐私

`data/siteroll.db` 保存网站、结果与调用计数，`data/token.json` 保存 Google 授权令牌。令牌不返回给前端；在支持 POSIX 的系统上以 0600 权限保存。文件没有额外加密，在 Windows 上依赖你的用户文件权限；请保护电脑账户和磁盘。断开本地连接删除本地令牌，但不会撤销 Google 端授权，后者请在 Google 账号第三方连接页面撤销。

工具直接访问添加的网站及 Google API。网页不接入广告、分析统计或外部前端资源。只绑定 `127.0.0.1`，检查 Host、会话请求令牌及 Origin。拒绝私有/本机地址，并对每个跳转重新校验 DNS；**不应部署为公网抓取服务**，当前 DNS 校验没有固定解析 IP，公网多用户部署需要额外的 DNS 重绑定防护、网络隔离、账户系统与凭证加密。

本地 `data/`、`credentials.json` 已忽略。分享源码时不要分享这些文件。复制 `data/` 可备份自己的历史（同时包含授权凭证，须安全保存）。

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

定时检查、中英切换、可配置批次上限、gzip sitemap、桌面打包。先验证实际使用流程，再扩展。

MIT License.

## 收录统计与变化报告

每站统计使用最近一次 GSC 检查，不混入配置检查。发现数来自当次成功读取的 sitemap；成功查询、失败、未检查分别显示，未知状态不计为未收录。旧版本的失败记录可能没有发现数，显示“未知”。新检查中断时仍保留发现数与部分结果。

对比上次同站、同类检查，仅比较两次都有明确结果的相同网址。首次检查建立基线；本次新增的网址表示“首次检查”，不是刚刚被 Google 收录。某网址不在本次结果中不会被判断为失去收录。Google 状态变化也不代表工具确定了原因。
