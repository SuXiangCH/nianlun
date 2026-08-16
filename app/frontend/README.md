# Nianlun Frontend

这是年轮 API Server 的前端 MVP，放在 `app/frontend`，采用 React、TypeScript 和 Vite。界面包含三个工作区：

- 对话：选择应用，使用 `/api/v1/apps/{id}/chat` 的 SSE 接口流式提问，并展示检索片段。
- 知识库：创建知识库、查看文档数量和状态、上传 Markdown 文件。
- 应用：创建应用，绑定可用知识库，配置 provider、model 和检索模式。

## 启动

先启动 API：

```bash
uv run uvicorn app.api_server.main:app --reload
```

另开终端启动前端开发服务：

```bash
cd app/frontend
npm install
npm run dev
```

打开 <http://127.0.0.1:3000>。API 默认使用 `http://127.0.0.1:8000`，也可以通过 `VITE_API_BASE` 配置；浏览器运行时仍兼容 `window.NIANLUN_API_BASE` 覆盖地址。

## 设计原则

前端以工作台为第一屏，不设置营销型首页。导航和页面层级围绕“应用对话、知识沉淀、应用配置”组织；对话是默认入口，知识库和应用是支撑对话的管理面。当前没有用户鉴权，因此界面只表达单工作区 MVP 的资源关系。API 调用和流式协议位于 `src/api`，三个工作区分别位于 `src/features`。
