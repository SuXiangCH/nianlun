# 图片存储：本地文件与 S3

[返回总览](../answer_images_design.md) · [完整验收清单](08_validation.md#blob-storage-validation)

## 职责与范围

为 03 的历史图片归档提供统一存储端口，默认本地文件，可配置 S3 兼容对象存储，包括 MinIO。
数据库保存资源身份、引用、来源和状态，存储后端保存原始二进制。两种后端都支持 09 的 URL/Data URL 投影。
控制台和公开客户端访问 Nianlun，由服务端代理读取；不要求浏览器或上游模型连接 MinIO。

本次只扩展答案图片的长期归档，不把原始上传、树索引、图片目录 bundle 或 snapshot 全部迁入 S3。
01 的 generation/manifest 仍是本地工作区文件，继续遵守维护锁与本地版本发布规则。
S3 后端不等于整个应用已经支持跨主机部署；当前 SQLite、checkpoint 和工作区仍要求原有部署边界，
不能用多份独立 SQLite 共享一个 bucket。MinIO/S3 支持与本地后端随第一阶段归档交付。

## 计划改动位置

下表为完整改动范围；Pillow/boto3 依赖由项目配置与锁文件管理。

| 类型 | 文件 | 计划改动 |
| --- | --- | --- |
| 新增 | `app/api_server/storage/image_blob_store.py` | ImageBlobStore Protocol、严格存储定位与结果类型 |
| 新增 | `app/api_server/storage/__init__.py` | 导出稳定存储端口，不在导入期间初始化 SDK |
| 新增 | `app/api_server/storage/filesystem_image_blob_store.py` | 受控目录、原子落盘及文件句柄清理 |
| 新增 | `app/api_server/storage/s3_image_blob_store.py` | boto3 的 S3 兼容适配器、限时 IO、关闭响应流 |
| 新增 | `app/api_server/services/answer_image_service.py` | 后端选择、来源验证、上传预约/读取租约/GC 编排 |
| 新增 | `app/api_server/repositories/answer_images.py` | 同一数据库下的状态 CAS、失效退役、引用、租约和回收声明 |
| 新增 | `app/api_server/database/models/answer_images.py` | 不可变 profile 定位、object key、对象状态、租约与回收记录 |
| 修改 | `app/api_server/database/migrations.py` | 向前迁移及存量本地 blob 定位兼容 |
| 修改 | `app/api_server/services/container.py` | 注册存储后端，在服务生命周期中关闭 client |
| 修改 | `app/api_server/config/settings.py`、`.env.example` | 后端、endpoint/bucket/region、超时及凭据配置 |
| 修改 | `pyproject.toml`、`uv.lock` | 引入并锁定 boto3；本地模式不初始化 S3 client 或联网 |
| 修改 | `docs/application_api.md` | 单机/MinIO 配置、bucket 权限、备份及切换限制 |

Agent 核心继续依赖已有稳定检索端口，不导入 boto3、Web settings、SQLAlchemy 或对象路径。

<a id="storage-port"></a>

## 存储端口与定位

`ImageBlobStore` 只提供受类型约束的 `put`、`open`、`stat`、`delete`。文件/对象 IO 在适配器中，
来源鉴权、引用计数、事务和 GC 决策在 service/repository；不把“检查数据库无引用”塞入适配器。
输入对象定位只能来自服务端已登记的 blob，不能来自模型、Markdown 或用户自报路径。

| 定位字段 | 约定 |
| --- | --- |
| `backend` / `storage_profile_id` | filesystem 或 s3，以及稳定的存储配置身份；不含密钥 |
| `object_key` | 服务端生成的相对 key，不是完整 URL；本地路径相对 settings 的归档根目录 |
| `version_id` | S3 versioning 开启时记录实际版本；关闭时为空 |
| `sha256` / `size_bytes` / MIME / 尺寸 | 经过 07 校验的原始字节元数据，不因传输或后端改变 |

S3 key 使用 `answer-images/<knowledge_base_id>/<blob_uuid>/<sha256>.<extension>`。
摘要用于去重，随机 blob 身份用于隔离不同上传/回收生命周期，不重复使用已经删除或回收中的 key。
同知识库、同摘要只允许一个活动 blob，部分唯一索引仅覆盖 uploading/ready；
retired/deleting/deleted 不参与去重，不阻塞新 UUID/key 的归档。retired 表示已禁止复用、等待物理回收，
与 deleting 的已领取回收任务、deleted 的已确认删除分开。三者都不能恢复为 ready；
回收记录保留原 key/profile，以处理迟到 IO，替代 03 原先无状态的唯一约束。
不依赖 bucket 全量 list 实现在线查找或去重，不用 S3 ETag 代替 SHA-256。

上传前在本地有界文件/缓冲中验证格式、长度及摘要。上传到唯一 key 后，首次提交前通过有界 GET 校验
实际远端字节；HEAD/stat 用于存在性与大小筛查，不足以证明内容正确。读取同样完整验证大小/摘要后，
才把临时文件句柄交给图片响应或 Base64 编码器，避免输出一半才发现摘要不符。
临时缓存计入既有单图/本轮预算，响应结束或取消后清理；不会保存为新的可信来源。

## 写入、复用与回收

文件系统由 03 的线程锁/POSIX 文件锁协调业务状态；适配器使用同目录临时文件、fsync 与原子硬链接发布，避免覆盖已有不可变对象。
S3 的 put/get/delete 在数据库事务外执行；所有服务/GC 使用同一 SQLite 的 CAS 与租约协调，
不能靠不同主机上的 POSIX 文件锁或“等待 24 小时”保证安全。首版不引入独立分布式锁服务。

1. **预约**：在短数据库事务创建 uploading blob、唯一 key、owner token 和租约，随后上传。
   同摘要并发上传通过唯一约束选出 owner，其他请求有界等待后复查，不能偷用 uploading 对象。
2. **提交**：上传/校验成功后，在完成写事务复查 owner token、租约、来源和当前状态；
   仅 uploading 的合法 owner 可原子改为 ready，并与消息、附件、run/授权共同提交。
   若复用 ready 对象，先取得读取租约并验证字节，完成事务再检查 ready 状态及租约，才能增加引用。
3. **失败/退役**：提交失败或请求取消不产生有效附件。owner 可用 token 将自己的 uploading CAS 为 retired 并撤销上传租约；
   崩溃留下的 uploading 在租约失效后，由新请求或恢复 worker 复查并 CAS 退役。退役与同摘要新预约可在同一短事务完成，
   不等 24 小时宽限或远端删除；旧 owner 即使迟到也不能续租、提交或改变新对象。
   超时不能被当作“远端一定没写入”；旧预约、key 和回收责任始终保留。单请求对同摘要最多建立一次替代预约，
   再遇到竞争或上传失败按有界等待/固定附件错误结束，不在内部循环不断创建新对象。
4. **领取 GC**：短写事务内复查无附件引用、无有效上传/读取租约及宽限已过，CAS 为 deleting，登记回收 claim。
   普通零引用 ready 按原孤儿规则回收；retired 按退役时间起算宽限，失效 uploading 须先走上述退役规则。
   deleting 禁止新引用/读取，也不因 claim 超时恢复为 ready；后续 GC 只能续领删除，不得与新写入复用同一个 key。
5. **删除**：事务外删除确切 key/版本，成功或确认对象不存在后，事务内把记录改为 deleted。
   中断/权限失败保留 deleting 重试；同摘要新归档使用另一 UUID/key，无须等待旧对象删除，
   迟到的 DELETE 只能命中原 blob 的确切定位，不能按摘要查询并删除当前活动对象。

上传/读取租约默认 5 分钟，活跃操作每 30 秒续租；IO 超时/有限重试必须在租约与请求截止时间内。
丢失 owner/读取租约的操作不得提交附件或披露字节，须重新授权并获得有效租约，否则失败。
上传预约在远端 PUT 前提交；过期上传即使迟到写入，也只能落到原 key，永远不能再转成 ready。
回收凭证保留 key，后台按宽限周期复查迟到对象并重复删除，不能仅因曾 DELETE 成功就立即丢掉记录。
初版删除所有已登记/迟到对象的责任属于应用 GC，不依赖未证明安全的 bucket 生命周期自动清理。

<a id="blob-recovery"></a>

### 失效对象与重新归档

ready 的确切对象确认不存在，或完整读取后大小/摘要不符时，在短事务内按预期 blob 身份、定位和 ready 状态 CAS 为 retired。
同事务将其附件改为固定原因的不可用状态、解绑 blob 并撤销关联 grant，保留来源和正文占位。
并发读取/复用在最终检查时必须看到退役状态并拒绝；已通过最终判定并开始传输的响应仍按原访问规则结束。
403、凭据错误、限流和网络超时不能作为“对象损坏”证据，不能据此退役或分配替代对象。

新归档请求若仍持有有效 snapshot/generation，并已按 07 校验原始字节与预期摘要一致，
可在退役事务内预约新 blob/key，按正常上传、GET 验证和完成事务提交新附件。
没有可验证的原始字节时只返回不可用；历史读取、幂等重放不从当前文档找替代图、不发起修复上传。
旧附件不自动改绑新 blob，已撤销来源/grant 不因同摘要重新出现而恢复；旧 key 的 GC 与新归档相互独立。
这样新请求不会被损坏且仍有历史附件的 ready 记录永久阻塞，也不改变已完成历史的绑定语义。

S3 读取在短事务内校验来源、期限和归属并获取 ready blob 的读取租约，再执行有界 GET。
GET 校验完、图片响应或 Data URL 生成前再次检查权限/租约；此时是最终访问判定点。
之后的客户端传输可结束，删除后的新读取失败；不在客户端慢速传输期间持有数据库写事务或 POSIX 锁。
读取流、临时文件和租约在正常、失败和取消时释放；崩溃留下的租约由超时回收。

对象存储与 SQLite 没有跨存储事务。上传后未提交、删除后未更新数据库均是可重试的已知状态，
不能伪装成原子提交；也不能在 S3 异常时静默转写本地目录，造成同一 blob 身份指向不同内容。
对象缺失/摘要不符作为附件不可用；服务端凭据/权限配置错误、超时按固定原因记录，
文字可继续的附件错误遵守 03，未知程序错误及数据库失败仍失败，不输出存储 URL、凭据或原始 SDK 异常。

## 配置、安全与后端切换

| 配置字段（环境变量按项目规则映射） | 默认与限制 |
| --- | --- |
| `answer_image_storage_backend` | filesystem；仅允许 filesystem/s3 |
| `answer_image_storage_profiles` | 默认空的严格 profile 映射；每项按 backend 校验本地根目录或 S3 定位、连接及凭据/角色配置，拒绝未知字段；密钥不持久化 |
| `answer_image_storage_profile_id` | 选中已配置 profile；未配置映射时由平铺字段生成稳定的 filesystem-default 或 s3-default，定位仍须匹配数据库指纹 |
| `answer_image_s3_endpoint_url` | 空表示 AWS 标准 endpoint；MinIO 必须填写受信任服务地址，不从 HTTP Host 推导 |
| `answer_image_s3_bucket` / `answer_image_s3_region` | s3 模式必填；bucket 预先由运维创建，不自动创建或设为公开 |
| `answer_image_s3_addressing_style` | auto，允许 path/virtual，MinIO 可选择 path |
| `answer_image_s3_access_key_id` / `answer_image_s3_secret_access_key` / `answer_image_s3_session_token` | 前两项成对配置，token 可选；也可使用 SDK 标准角色凭据，不回退到 CLI 模型配置 |
| `answer_image_s3_connect_timeout_seconds` / `answer_image_s3_read_timeout_seconds` | 5 / 30，正数，配合请求取消 |
| `answer_image_s3_total_max_attempts` | 3，有限重试；应用不叠加无限循环 |

生产使用 TLS 并校验证书，可配置可信 CA；本地 MinIO 可显式使用 HTTP，不默认关闭 TLS 验证。
仅给专用 bucket/prefix 的 Get/Put/Delete/Head 所需权限；禁止公开读，versioning 开启时需精确版本读/删权限。
版本化 bucket 删除确切 version，不能只写 delete marker 后声称物理回收成功。
迟到 PUT/超时上传尚未登记 version_id 时，由回收 worker 对该应用生成的唯一 key 枚举确切版本并逐个删除，
需授予对应版本枚举权限；不扫描/清空用户其他 key。回收记录保留以发现更晚出现的版本。
对象元数据只放技术字段，不写敏感图题、用户提问或应用 Key。SDK client 延迟构造，关闭 StreamingBody，
使用同步 boto3 时在 worker/线程中执行，导入、普通单元测试及本地后端不能触发网络或凭据探测。

每个 blob 绑定创建时的 profile，后续更换默认 backend 只影响新归档；旧 filesystem profile 继续可读。
profile ID 对应的 endpoint/bucket/prefix 等定位配置一经登记即不可变；包括只剩 retired/deleting/deleted 回收记录时，
也不能改指别处。启动检查配置指纹，不匹配明确拒绝；更换存储必须登记新 profile ID 并切换默认值。
数据库持久化不含密钥的 profile 定位与指纹，运行配置按 profile ID 提供凭据或角色引用；旧 profile 在尚有读取或回收责任时
必须继续可解析且具备所需权限，不能只配置当前默认后端。缺少旧配置时启动明确失败，不跳过旧对象清理。
登记新 profile 使用严格配置映射（`answer_image_storage_profiles`，环境变量可传 JSON），
`answer_image_storage_profile_id` 选择默认；上述平铺 S3 字段仅作为首次单 profile 的配置简写，不能覆盖已登记定位。
显式映射存在时以其为完整配置来源，不将平铺字段混入各 profile；默认 profile 必须存在且与所选 backend 一致，否则拒绝启动。
默认 filesystem 模式且无历史 S3 责任时仍不构造 S3 client；有历史 S3 责任时按需装配旧 profile 并延迟联网。
首版保留所有已登记 profile 与回收凭证，不自动按年龄移除；停止旧 profile 的迟到对象检查须另行定义并显式授权，
不能把一次 DELETE 成功或当前无活动 blob 视为清理责任终结。
密钥轮换不改变 profile 身份。跨 bucket/后端的存量迁移须另行授权、复制并验摘要后更新定位，不能改一个环境变量就当迁移完成。
备份应包含 SQLite、工作区和相应私有 bucket 数据；本次不迁移或删除任何已有文件/对象。

## SDK 依据与验收

拟使用 [boto3 S3 文档](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html)
和 [client 配置](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/configuration.html)；
MinIO 通过 endpoint_url/region/addressing_style 接入。不同兼容服务的能力必须经过集成验收，
不依赖厂商专有原子 rename、ETag 格式或未验证的条件写入语义。

默认测试使用 fake BlobStore/SDK stub 与临时 SQLite，不访问真实对象服务；
MinIO 集成检查使用显式启用、独立临时 bucket，绝不清空用户 bucket。具体场景见 [08](08_validation.md#blob-storage-validation)。
IO 基础层的 `delete(all_versions=True)` 每次最多处理 16 页版本（可在适配器配置中调整，范围 1–1000）；
达到上限明确失败，后续由 GC 重试，不能据此将 blob 标记 deleted。只按确切 key 删除版本或 delete marker，不删除同前缀其他对象。
