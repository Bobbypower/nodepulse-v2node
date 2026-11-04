# 节点证书命令契约（本地候选，未部署）

本命令供中央签发、固定SSH身份、调度和独立TLS验证对接。只有受信主机部署通道可以调用；共享节点token不授予这些命令权限，节点不接收DNS/Cloudflare API凭据。Linux服务和CLI须使用同一实际effective/filesystem UID。配置/账户目录0700，profile、账户及全部私钥0600；拒绝软链接、私钥硬链接、外来所有权及可被其他用户修改的父目录。

## 自动批准配置

后台调用 `v2node certificate profile-init --profile-id origin_900 --node-id 900`，stdin只接公开JSON：

```json
{"profile_id":"origin_900","domain":"node.example.invalid","cert_file":"/etc/v2node/vless900.cer","key_file":"/etc/v2node/vless900.key","trust_mode":"cloudflare_origin_ca","client_route":"cdn_only"}
```

后台自动创建profile，用户无需建主机文件。自动初始化仅接受该节点的 `/etc/v2node/vless<ID>.cer/.key`，ID1–9007199254740991。相同ID/规范化内容幂等，已有ID不可静默改写；策略/域名变化要新profile ID。已有旧批准配置仍可读，旧四字段profile默认public_ca且哈希保持兼容。profile目录固定 `/etc/nodepulse/certificates/profiles`，正式二进制无环境变量改目录/信任池入口。

trust_mode为public_ca（默认）或cloudflare_origin_ca；client_route为空、dns_split或cdn_only。Origin CA必须显式cdn_only；dns_split在CSR前拒绝Origin。域名是小写具体ASCII/Punycode DNS名，不接IP/通配符。路径须绝对、规范化、不同。profile ID为1–80个字母/数字/下划线/连字符，首字符字母或数字。身份/路径/信任策略进入profile_sha256。

## 命令和严格输出

```text
v2node certificate profile-init --profile-id origin_900 --node-id 900
v2node certificate profile --profile-id origin_900
v2node certificate csr --profile-id origin_900 --request-id initial_job_900
v2node certificate install --profile-id origin_900 --request-id initial_job_900
v2node certificate inspect --profile-id origin_900
v2node certificate rollback --profile-id origin_900 --expected-sha256 <当前叶指纹> --target-sha256 <确切旧叶指纹>
```

通过固定程序路径和参数数组调用；新安装使用审核SHA派生的 `/usr/local/lib/nodepulse/v2node/<sha>/v2node`。全局路径仅供已批准的兼容操作。程序SHA和SSH主机指纹均须在执行前独立核验。request-id为8–80个字母/数字/下划线/连字符，全任务保持不变。

- profile-init/profile严格11字段：state/profile_id/domain/cert_file/key_file/profile_sha256/runtime_uid/runtime_uid_source/service_uid_verified/trust_mode/client_route。state=profile；runtime_uid_source=cli；service_uid_verified=false。profile无证书也可只读预检，要求已存在0600而不chmod。CLI UID不是守护进程身份，不能据此跳过真实服务核验。
- csr只输出PEM CSR。P256私钥留节点 `key_file.pending-<request-id>`（0600），缓存CSR、域名、profile绑定、任务和原证书代次，重复返回相同字节。安装成功后同任务csr从活动私有PEM恢复原CSR并验证签名/配对，不再建key或pending。
- install的stdin只接公开PEM证书链，最多4MiB；验证域名、有效期、私钥配对和所选信任策略。不得包含私钥/凭据。pending的原代次绑定使过时签发无法覆盖较新证书。
- install/inspect/rollback严格10字段：state/profile_id/request_id/profile_sha256/sha256/not_after/runtime_uid/tls_handshake_verified/generation_bound/effective_not_after。state=persisted；sha256为叶DER指纹；request_id来自实际私有配对元数据，未绑定legacy为空且generation_bound=false；tls_handshake_verified=false。时间为UTC RFC3339字符串。effective_not_after为允许验证路径中最优路径的最早到期点，续期不能只看长寿命叶证书。
- rollback要求当前/目标确切指纹，已是目标则幂等成功；只恢复.previous中匹配且符合所选profile信任/域名/有效期的配对。跨类型恢复须用原profile ID，不跳过信任。其他活动代次变化即拒绝。

退出0成功、1失败。stderr仅诊断，不能按文本制定写入重试。SSH中断或结果未知时先inspect/独立TLS核对；不换request-id重签。profile/inspect均不证明服务加载，persisted不能显示为已生效。

## 受控Origin CA

public_ca按系统公共CA验证，并显式排除已知CF Origin CA锚。cloudflare_origin_ca只用内置固定DER指纹的官方RSA/ECC根，操作内独立根池，不加入系统信任、不skip-verify、不接自选CA路径。当前两根均于2029-08-15 17:00 UTC到期，叶申请15年不代表整链15年。根指纹/下载来源见cloudflare-origin-root-manifest.json。

中央CF API或DNS-01 broker只收到节点CSR，返回公开链；DNS/API密钥不下发节点。CF-only可用Origin CA，直连/dns_split必须公共CA。[Cloudflare官方Origin CA说明](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/)明确其用于CF到源站，普通直连客户端不会默认信任。

## 原子更新、热加载和恢复

v2node安全写入器与Xray成对读取器须成套发布。key_file是0600完整私有PEM（证书链+私钥），一次原子rename提交整对；cert_file仅公开镜像。旧分离文件先安全升级旧配对，候选先验配对/域名/有效期，.previous保留旧完整配对。预提交失败保留旧配对；rename已完成但目录fsync不确定时明确报告published待核对，不虚构回滚。其他独立按两个文件读取的程序不在此原子性保证内。

跨进程flock防并发写入；幂等安装不覆盖原回滚备份。安装成功移除pending，但无自动放弃/清理接口，不能通配删除私钥。Xray每5秒重试加载，读取/验证失败继续旧内存证书；新连接取新不可变证书，旧TLS连接延续。OCSP网络不持握手锁，带超时/大小上限。控制面须按真实端口/SNI/所选根/H2核验新叶指纹，建议至少15秒窗口，失败回滚后仍独立握手。

## 首次安装和发布边界

后台调用的无证书两阶段流程及25字段收据见BOOTSTRAP-CALL-CONTRACT.md。准备/签发/导入不要求已运行服务，只有绑定准备收据后才启动新单元；普通续期保持active条件。首次准备拒绝任何未认领的旧证书、配置或unit，不删除或接管。用户不用手工建profile或在CF网页手工签证。

原生HTTP-01保留并修复账户保存/取消。此候选明确禁用节点原生DNS模式和DNSEnv，中央DNS-01/file模式承担新自动化；发布前须盘点和迁移既有原生DNS配置。本地CLI、合成CA、实际TLS/H2、原子更新及回滚已验证；真实CA/DNS、SSH/systemd首次安装、JP4/CF和普通客户验收仍未完成。没有提交、推送或部署。
