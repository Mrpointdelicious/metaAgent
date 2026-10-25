# 本地报告图片部署

浏览器地址与容器内部访问地址分别配置。本地 Dify 部署使用稳定的 localhost 入口，避免主机 IP 变化后已生成报告无法展示。

后端运行环境：

```dotenv
IReGoTools__ReportImage__PublicBaseUrl=http://localhost:15043
HospitalOperations__PublicBaseUrl=http://localhost:15043
```

MetaAgent 运行环境：

```dotenv
META_AGENT__ARTIFACT_PUBLIC_BASE_URL=http://localhost:15043
```

MetaAgent 仅将此明确配置的 origin 下患者/医院运营的 `report-artifacts/{ref}/content` 路径映射到对应 `AI_WEBAPI_*_BASE_URL` 的内部 origin，保留查询参数，验证图片类型，不跟随重定向、不发送工具鉴权头。返回给前端的 URL 保持 localhost。

`workflows/dify/present.py` 是已发布展示节点源码；即使回答已含普通报告链接，仍展示经过 `artifact_ready` 确认的图片；同一图片仅插入一次。

localhost 适用于当前浏览器与 Docker 位于同一主机的部署。供其他电脑访问时，应将后端两个 PublicBaseUrl、MetaAgent 的 ARTIFACT_PUBLIC_BASE_URL 与 Dify 展示节点的可信域名同时改为稳定的外部地址；不要填写会随网络变化的主机 IP。

生成患者报告还要求可信患者绑定能够唯一解析到相应设备账号。映射歧义需在业务侧明确关联，图片地址修复不会代替患者映射。
