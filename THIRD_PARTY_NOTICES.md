# 第三方依赖说明

本项目通过包管理器安装第三方开源依赖，不把这些依赖的源码直接复制到仓库中。各依赖仍适用其自己的许可证。

后端主要依赖包括 FastAPI、HTTPX、OpenAI Python SDK、Pydantic Settings、SQLModel、Uvicorn 和 WebSockets。前端主要依赖包括 React、React Router、TanStack Query、Zod、Lucide React、React Markdown 和 remark-gfm。测试与构建使用 pytest、Ruff、mypy、TypeScript、Vite、Vitest、oxlint 和 Playwright。

前端的完整版本和完整性校验记录在 `frontend/package-lock.json`。后端的锁定解析记录在 `backend/uv.lock`，可供 pip 直接安装的哈希锁定清单位于 `backend/requirements.lock`。发布或再分发依赖本身时，请同时遵守对应包中附带的版权和许可证文本。

项目界面使用系统字体，不打包第三方字体、预制语音或外部图片素材。模型与语音能力通过阿里云百炼服务调用，服务使用、费用和生成内容适用阿里云平台的现行条款，不属于 MIT License 的授权范围。
