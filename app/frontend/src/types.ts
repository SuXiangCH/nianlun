export type View = "chat" | "knowledge" | "applications" | "models";
export type KnowledgeBaseStatus = "creating" | "ready" | "indexing" | "error";
export type AgentVersion = "v0" | "v1";

export interface Application {
  id: string;
  name: string;
  description: string;
  knowledge_base_id: string;
  llm_model_id: string | null;
  provider: string;
  agent_version: AgentVersion;
  /** Legacy API field retained for reading existing applications. */
  search_mode: "scan" | "fts";
  created_at: string;
  updated_at: string;
  /** Optional when reading legacy application fixtures; current API always supplies both. */
  config_version?: number;
  clarification_enabled?: boolean;
}

export interface ApplicationUpdateRequest {
  expected_config_version: number;
  name: string;
  description: string;
  knowledge_base_id: string;
  llm_model_id: string | null;
  agent_version: AgentVersion;
  clarification_enabled: boolean;
}

export interface ApplicationRelease {
  id: string;
  application_id: string;
  sequence: number;
  draft_config_version: number;
  config_schema_version: number;
  config_hash: string;
  description: string;
  config: PublishedApplicationConfig;
  created_at: string;
}

export interface PublishedApplicationConfig {
  schema_version: 1;
  application_id: string;
  agent_version: AgentVersion;
  llm_model_id: string;
  model: string;
  provider: string;
  base_url: string;
  context_window_tokens: number;
  knowledge_base_id: string;
  knowledge_update_policy: "latest_committed";
  clarification_enabled: boolean;
  temperature: number;
  thinking: boolean | null;
  loop_guard: {
    enabled: boolean;
    repeat_warn_threshold: number;
    repeat_hard_limit: number;
    no_progress_warn_rounds: number;
    no_progress_hard_rounds: number;
    model_round_warn_threshold: number;
    max_model_rounds: number;
    tool_round_warn_threshold: number;
    max_tool_rounds: number;
    total_tool_call_warn_threshold: number;
    max_total_tool_calls: number;
    per_tool_hard_limits: Record<string, number>;
    recursion_limit: number;
    case_timeout_seconds: number;
  };
  prompt_version: number;
  tool_schema_version: number;
  checkpoint_schema_version: number;
  v1_prompt_hash: string;
}

export interface ApplicationPublication {
  application_id: string;
  api_channel_created: boolean;
  enabled: boolean;
  active_release: ApplicationRelease | null;
  deployment_version: number;
  last_deployed_at?: string | null;
  config_version: number;
  has_unpublished_changes: boolean;
  dependencies_ready: boolean;
  dependency_error: string | null;
  draft_ready: boolean;
  draft_error: string | null;
}

export interface ApplicationDeploymentEvent {
  id: string;
  application_id: string;
  channel_type: "api";
  action: "deploy" | "enable" | "disable";
  previous_release_id: string | null;
  release_id: string | null;
  previous_sequence: number | null;
  sequence: number | null;
  enabled: boolean;
  deployment_version: number;
  created_at: string;
}

export interface ApplicationApiClient {
  id: string;
  application_id: string;
  name: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface ApplicationApiKey {
  key_id: string;
  client_id: string;
  name: string;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface ApplicationRun {
  run_id: string;
  app_id: string;
  client_id: string;
  release_id: string;
  conversation_id: string;
  status: string;
  error_type: string | null;
  error_status: number | null;
  error_message: string | null;
  result: PublicChatResponse | null;
  created_at: string;
  updated_at: string;
  knowledge_base_content_version: number;
  request_id: string | null;
  duration_ms: number;
  usage: Record<string, number> | null;
}

export interface PublicChatResponse {
  app_id: string;
  release_id: string;
  run_id: string;
  conversation_id: string;
  message_id: string;
  answer: string;
  retrieved_snippets: SourceSnippet[];
  usage: Record<string, number> | null;
  ttft_ms: number | null;
  clarification: ClarificationRequest | null;
  knowledge_base_content_version: number;
}

export interface ApplicationPage<T> { items: T[]; total: number; }

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  status: KnowledgeBaseStatus;
  document_count: number;
  summary_enabled: boolean;
  heading_recovery_enabled: boolean;
  llm_model_id: string | null;
  workspace_dir: string;
  workspace_relpath: string | null;
  content_version: number;
  fts_status: "disabled" | "pending" | "building" | "ready" | "failed";
  fts_revision: number | null;
  fts_target_revision: number | null;
  fts_collection: string | null;
  fts_error: string | null;
  vector_status: "disabled" | "pending" | "building" | "ready" | "failed";
  vector_enabled: boolean;
  vector_revision: number | null;
  vector_target_revision: number | null;
  vector_collection: string | null;
  vector_error: string | null;
  embedding_model_id: string | null;
  vector_model_id: string | null;
  vector_model_updated_at: string | null;
  vector_dimension: number | null;
  vector_progress_stage: string | null;
  vector_documents_total: number | null;
  vector_documents_completed: number | null;
  vector_records_processed: number | null;
  created_at: string;
  updated_at: string;
}

export type DocumentStatus = "uploaded" | "parsing" | "parsed" | "indexing" | "ready" | "failed" | "deleted";
export type DocumentParser = "native_markdown" | "mineru";

export interface DocumentArtifact {
  id: string;
  document_id: string;
  kind: "original" | "result_zip" | "full_markdown" | "content_list" | "layout" | "model" | "asset" | "parse_chunk_source" | "parse_chunk_result" | "normalized_markdown" | "page_map" | "enriched_markdown" | "tree" | "diagnostics";
  relpath: string;
  mime_type: string;
  size_bytes: number;
  pipeline_generation: number;
  created_at: string;
}

export interface DocumentParseTask {
  id: string;
  document_id: string;
  provider: "mineru";
  api_mode: "saas_precision" | "self_hosted";
  attempt: number;
  pipeline_generation: number;
  chunk_index: number;
  chunk_count: number;
  source_page_start: number | null;
  source_page_end: number | null;
  data_id: string;
  batch_id: string | null;
  task_id: string | null;
  model_version: "pipeline" | "vlm";
  state: "created" | "uploading" | "waiting-file" | "pending" | "running" | "converting" | "done" | "failed" | "canceled";
  dispatch_state: "queued" | "leased" | "waiting" | "succeeded" | "failed" | "canceled";
  available_at: string | null;
  next_poll_at: string | null;
  extracted_pages: number | null;
  total_pages: number | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface KnowledgeBaseDocument {
  id: string;
  knowledge_base_id: string;
  original_filename: string;
  file_extension: string;
  mime_type: string;
  size_bytes: number;
  parser: DocumentParser;
  status: DocumentStatus;
  pipeline_generation: number;
  current_stage: "parse" | "normalize" | "enrich" | "publish" | "index" | "complete";
  stage_state: "queued" | "running" | "succeeded" | "partial" | "failed" | "skipped";
  failed_stage: "parse" | "normalize" | "enrich" | "index" | null;
  progress_completed: number | null;
  progress_total: number | null;
  progress_unit: "pages" | "chunks" | "nodes" | "documents" | null;
  warnings: Array<Record<string, unknown>>;
  parsed_content_version: number | null;
  published_content_version: number | null;
  progress: { completed: number | null; total: number | null; unit: "pages" | "chunks" | "nodes" | "documents" | null };
  parse: { chunks_completed: number; chunks_total: number; pages_completed: number; pages_total: number };
  error_code: string | null;
  error_message: string | null;
  latest_task: DocumentParseTask | null;
  artifacts: DocumentArtifact[];
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface DocumentIndexNode {
  title: string;
  node_id: string;
  line_num: number;
  summary?: string;
  prefix_summary?: string;
  nodes?: DocumentIndexNode[];
}

export interface DocumentIndexTree {
  doc_name: string | null;
  doc_description: string | null;
  line_count: number | null;
  structure: DocumentIndexNode[];
}

export interface BatchUploadFileResult {
  filename: string;
  ok: boolean;
  document_id?: string;
  idempotent_replay?: boolean;
  status_code?: number;
  error?: string;
}

export interface BatchUploadResult {
  knowledge_base: KnowledgeBase;
  files: BatchUploadFileResult[];
}

export type ModelTestTarget = "llm" | "embedding" | "parser";

export interface ModelConfigTestRequest {
  target: ModelTestTarget;
  model?: string | null;
  base_url?: string | null;
  api_key?: string | null;
  dimension?: number | null;
  api_mode?: "saas_precision" | "self_hosted";
}

export interface ModelConfigTestResult {
  target: ModelTestTarget;
  ok: boolean;
  message: string;
}

export type ModelKind = ModelTestTarget;

export interface ModelProfile {
  id: string;
  kind: ModelKind;
  name: string;
  model: string | null;
  context_window_tokens: number | null;
  base_url: string | null;
  api_key_configured: boolean;
  enabled: boolean;
  api_mode: "saas_precision" | "self_hosted";
  dimension: number | null;
  model_version: "pipeline" | "vlm";
  language: string;
  is_ocr: boolean;
  enable_table: boolean;
  enable_formula: boolean;
  page_ranges: string;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface ModelProfileRequest {
  kind: ModelKind;
  name: string;
  model: string | null;
  context_window_tokens: number | null;
  base_url: string;
  api_key?: string | null;
  enabled: boolean;
  api_mode: "saas_precision" | "self_hosted";
  dimension: number | null;
  model_version: "pipeline" | "vlm";
  language: string;
  is_ocr: boolean;
  enable_table: boolean;
  enable_formula: boolean;
  page_ranges: string;
  is_default: boolean;
}

export interface SourceSnippet {
  message_id?: string;
  source_order?: number;
  citation_id?: number;
  doc_id?: string;
  node_id?: string;
  doc_name?: string;
  line_spec?: string;
  line_num?: number;
  retrieved_text?: string;
  char_offset?: number;
  char_limit?: number;
  total_chars?: number;
  text_truncated?: boolean;
  content_version?: number;
  title?: string;
  document_name?: string;
  filename?: string;
  source?: string;
  content?: string;
  text?: string;
  snippet?: string;
  page_content?: string;
  path?: string;
  heading?: string;
  chunk_id?: string;
  id?: string;
  [key: string]: unknown;
}

export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_tokens: number;
}

export interface ToolCall {
  name: string;
  args?: Record<string, unknown>;
  elapsed_ms?: number | null;
  tool_call_id?: string | null;
  batch?: number | null;
}

export type AgentTraceStep =
  | { kind: "status"; event: string; message: string }
  | { kind: "agent_message"; message: string; round?: number; transitionName?: string };

export interface ChatDoneEvent {
  app_id: string;
  conversation_id: string;
  message_id: string;
  answer: string;
  route: string;
  retrieved_snippets: SourceSnippet[];
  tool_calls?: ToolCall[];
  trace?: AgentTraceStep[];
  usage?: TokenUsage | null;
  ttft_ms?: number | null;
  clarification?: ClarificationRequest | null;
}

export interface ClarificationRequest {
  question: string;
  clarification_type: string;
  context?: string | null;
  options?: string[];
  clarification_id?: string;
}

export interface ChatMessage {
  id: string;
  // Server-persisted message id. During streaming `id` stays the stable
  // client-side placeholder (so React does not remount the bubble when the
  // final `done` event assigns the real id); `server_id` records that real id
  // for matching against history-API records after a reload.
  server_id?: string | null;
  role: "user" | "assistant";
  text: string;
  pending?: boolean;
  stopped?: boolean;
  error?: boolean;
  tool_calls?: ToolCall[] | null;
  trace?: AgentTraceStep[] | null;
  keepTraceOpen?: boolean;
  streamRound?: number | null;
  streamTransitionName?: string | null;
  usage?: TokenUsage | null;
  ttft_ms?: number | null;
  sources?: SourceSnippet[];
}

export interface ConversationSummary {
  id: string;
  title: string;
  updatedAt: number;
}

export interface ConversationRecord {
  id: string;
  application_id: string;
  title: string;
  status: "active" | "archived" | "deleted";
  created_at: string;
  updated_at: string;
  last_message_at: string | null;
  message_count: number;
}

export interface ConversationMessageRecord {
  id: string;
  conversation_id: string;
  seq_no: number;
  role: "user" | "assistant";
  content: string;
  status: "pending" | "completed" | "failed";
  route: string | null;
  error_message: string | null;
  tool_calls?: ToolCall[];
  trace?: AgentTraceStep[];
  usage?: TokenUsage | null;
  ttft_ms?: number | null;
  created_at: string;
  updated_at: string;
  sources: SourceSnippet[];
}

export type ApiEvent = { event: string; data: Record<string, unknown> };
