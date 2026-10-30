# 首次无证书的节点两阶段安装契约 v1（本地候选，未部署）

节点侧实现位于 `E:/nodepulse-v2node/ops/install-nodepulse-v2node.sh` 与 `ops/edge_gateway/node_bootstrap.py`。控制面负责固定SSH主机身份、管理专用授权、任务排队、CA签发、独立TLS验证及最终发布。普通续期仍要求active服务；首次安装有独立阶段，不用CLI UID冒充守护进程身份。用户不需创建主机profile或手动签证。

## 已稳定的接口

两个安装阶段均须提供同一 `CERTIFICATE_PROFILE_ID`（1–80个字母/数字/下划线/连字符，首字母数字）和 `CERTIFICATE_REQUEST_ID`（8–80个字母/数字/下划线/连字符）。节点ID为1–9007199254740991。此候选只接受新建、未运行、无未认领部署文件的节点；旧65/66、23333和443不属于新任务。

后台通过现有受信部署通道设置以下公共参数，节点共享token沿现有受保护方式注入，禁止记录或回显：

```text
NODE_ID=900
NODE_PORT=8443
NODEPULSE_URL=<批准的面板地址>
V2NODE_RELEASE_BASE=<固定的已审核发布包地址>
V2NODE_SHA256=<该架构二进制SHA256>
EDGE_GATEWAY_PACKAGE_SHA256=<同一版本helper包SHA256>
ISOLATED_RUNTIME=1
CERTIFICATE_PROFILE_ID=origin_900
CERTIFICATE_REQUEST_ID=initial_job_900
```

1. **准备**：另加 `DEFER_START=1`，执行经过SHA校验的 `bash <installer-path>`。脚本取启用但隐藏节点的最终配置，限定VLESS/XHTTP/TLS、单个原生H2回源监听、每节点专用文件证书路径，锁定实际ListenIP/ListenPort。新二进制位于 `/usr/local/lib/nodepulse/v2node/<sha>/v2node`，不替换全局bin。全主机部署锁内，先将任务和文件摘要保存到 `/etc/nodepulse/bootstrap/v2node-900.json`（目录0700/文件0600），再安装该节点配置和unit。unit显式 `User=root`，准备后disable但不start。最后一行stdout为 `state:prepared` 的JSON。
2. **生成与导入**：使用准备收据的固定binary_path，参数数组调用 `certificate profile-init --profile-id origin_900 --node-id 900`，stdin是后台生成的公开profile；随后 `certificate profile`、`certificate csr --profile-id origin_900 --request-id initial_job_900`。中央CA仅接CSR，返回公开链后调用 `certificate install --profile-id origin_900 --request-id initial_job_900`。profile/domain/路径/策略须与DB、节点及准备收据一致。此阶段不要求服务active。
3. **激活**：同一安装参数加 `DEFER_START=0 BOOTSTRAP_BINDING_SHA256=<prepared.binding_sha256>`，再次运行同一审核installer。重新获取的面板配置、配置文件、unit文件、二进制、节点ID、域名、任务及profile必须与准备记录一致。profile/inspect必须证明证书属于同一任务和配置，信任链有效、CLI UID为0。激活不重写配置和unit；只enable/start这个未运行目标，不restart。稳定窗口、实际PID/effective UID/fs UID、程序摘要、配置参数、cgroup和目标socket归属均核验通过后，最后一行stdout为 `state:activated` JSON。
4. **独立验收**：activated仍为 `tls_handshake_verified:false`。控制面必须另做限定根/SNI/H2/叶指纹TLS握手，并以合成账号验证实际XHTTP收发，最后才移除隐藏并进入常规续期。Origin CA只能cdn_only；dns_split必须公共CA；外部回源不经Caddy。

prepared和activated都严格为25字段，见 `bootstrap-receipt.schema.json`、`bootstrap-receipt-examples.json`。例子仅为合成展示，node900、指纹及PID不能作为真实验收记录。`binding_sha256` 是固定身份字段的哈希。准确算法：对原始持久记录剔除state和binding_sha256后的全部字段，JSON按键排序、无多余空白、UTF-8序列化后取SHA256。service_started/service_uid_verified/runtime_pid/runtime_uid/tls_handshake_verified是动态响应字段，不进入binding。控制端不可重新构造手写收据代替核验。

## 查询命令

节点helper由同一个SHA校验过的 `nodepulse-edge-gateway.tar.gz` 安全解包到后台创建的root私有临时目录。后台可通过SSH参数数组执行：

```text
python3 -I -c 'import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("edge_gateway.node_bootstrap",run_name="__main__")' <verified-helper-root> status --node 900
```

status只核验并输出相同25字段，不保存状态、不启动服务。active时重新核验进程、UID、程序、配置、cgroup、socket和绑定证书。inactive但保留完整prepared/activated记录时输出prepared，可用原binding和同任务安全重试激活；reserved、文件缺失、配置变化、外部重启状态不明确则非零退出待核对。status内部自行获取主机部署锁，避免与installer并发；installer也会自行持锁，两者都不应再从外层重复加同一排他锁。

## 必须已有与自动建立的内容

- 必须已有：Linux/root、systemd、Python3、ss/flock/sha256sum、curl或wget、正常CA根、到面板/审核包源网络；后台须固定SSH主机指纹、独立管理token、最终节点/服务器/域名/zone/路由/端口及审核文件摘要。面板节点在准备前须启用且隐藏，使配置API可取；未完成证书或验收时不能公开发布。
- 自动建立：版本化程序、节点专属配置、显式root的目标unit、root私有任务记录；profile-init自动建0700/0600 profile，CSR自动建0600待签私钥。用户不用登录节点建这些文件。helper查询时的验证/解包由后台自动完成。
- **首次准备的拒绝行为**：若目标配置/unit文件已存在，或systemd解析到未认领的同名unit，直接拒绝。若 `/etc/v2node/vless900.cer`、`.key`、`.key.previous`、任何`.key.pending-*`或同前缀锁/文件已存在而无本任务保留记录，拒绝 `bootstrap_unowned_existing_certificate`，不读取私钥、不chmod、不覆盖、不采用，也没有force参数。不能删除它们后自动重试。路径父目录/文件软链接、硬链接、所有权或权限异常同样拒绝。
- 同任务准备重试仅接受原保留记录及精确匹配的文件；已有CSR或已安装证书时不要重新prepare，继续原签发/安装任务或query/activate。更换request/profile、端口、配置或二进制不能认领旧任务；需要显式受控迁移。

## 静态和实际身份核验

准备收据只含 planned_runtime_uid:0、runtime_uid:null、runtime_pid:null、service_uid_verified:false。读取真实目标unit内容和SHA，以及systemctl show的Id/LoadState/FragmentPath/User/Group/DynamicUser/DropInPaths/ExecStart/NeedDaemonReload；禁止drop-in、动态用户、非root/别的group、待reload或不同命令。实际服务启动后检查 `/proc/PID/status` 的effective/fs UID都为0，程序真实路径及摘要、完整argv、节点配置摘要/NodeID、准确system.slice cgroup、socket inode归属及进程开始标识在验证中未变化。端口可握手本身不能替代归属证明。

## 失败及不确定结果

prepare先保留任务身份，预提交失败可按同任务精确摘要恢复；只有已认领的目标会进入回滚范围。准备或签发失败不启动服务；版本化不可变程序不删除，避免影响使用它的其他单元。installer保存该目标原配置/unit和启用状态，激活失败停止本任务目标并恢复该目标，绝不调用其他节点或全局bin。原始任务记录、CSR和私钥保留供核对，不按通配符删除。

CLI安装回执丢失先inspect，同request-id可恢复原CSR，不生成新key，也不表示可以盲目重新POST CA。激活回执丢失先status：若已active且身份一致则记录结果，不重复启动；若已证明同任务配置完整且inactive，可同binding重试start；过渡状态或任何身份变化拒绝。跨证书类型回滚使用事先记录的旧profile及当前/目标确切指纹，回滚后仍需实际TLS验收。

## 本地结果及明确待办

`bootstrap-helper-tests.log`：97项helper/状态机/身份/真实Bash早期拒绝测试通过；installer契约测试及bash -n通过。Windows用临时文件和模拟systemd/proc验证编排状态；真实Linux证书CLI另有32项通过/1项独立封禁环境跳过，包含无旧证书的自动profile-init→CSR→导入→真实TLS/H2。不是97项真实systemd测试。

本机docker-desktop可执行Linux测试二进制，但不是可用的完整systemd发行环境，缺Python/常规bash，Docker daemon当前未启动；未启动/重置全局Docker以干扰其他会话。因此真实systemd两阶段安装、SSH固定身份、真实CA staging/CF、完整新v2node/XHTTP首次安装仍待隔离Linux VM或专用验收机验证；控制面首次安装状态机已接入默认worker，详见BOOTSTRAP-CONTROL-STATUS.md。没有操作真实节点、部署或改生产DNS。
