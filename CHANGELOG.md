# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- **批量操作 SSE 实时进度**：批量删除用户、批量变更套餐、批量重置密码新增 SSE 流式进度反馈，前端实时显示处理状态
- **批量变更套餐**：支持批量修改选中用户的 Kiro 订阅套餐
- **批量重置密码**：支持批量为选中用户发送密码重置邮件（email 模式）或 OTP（otp 模式）
- **批量删除用户**：支持批量删除选中用户，同时取消其 Kiro 订阅并从 Identity Center 移除
- **前端 SSE 工具类**：新增 `sseStream.ts` 用于处理 SSE 流式请求和进度解析
- **i18n 国际化**：新增批量操作相关中英文翻译

### Fixed
- **账号同步安全性**：`search_users` API 失败时抛出异常而非静默返回空列表，避免误判为"账号无用户"导致数据被清空
- **用户状态推导**：统一 `_derive_status` 方法，正确处理 `Active` 和 `UserStatus` 字段缺失的情况
- **Modal 确认框与进度蒙版冲突**：批量操作确认后立即关闭 Modal，避免与进度蒙版叠加

### Changed
- **SSO 区域支持**：前端 SSO Region 下拉框支持所有 AWS regions，不再限制为特定区域
- **剪贴板兼容性**：新增 `copyToClipboard` 方法，兼容非 HTTPS 环境下的复制功能
- **并发控制**：批量操作支持配置并发数（默认 5，最大 20），AWS 调用在线程池并发执行，数据库写入串行处理

## [Previous Releases]

_ Historical releases were not tracked in this file._
