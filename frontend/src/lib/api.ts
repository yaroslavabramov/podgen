import ky from "ky";

const apiBase = process.env.NEXT_PUBLIC_API_URL || "/";

export const api = ky.create({
  prefixUrl: apiBase,
  hooks: {
    beforeRequest: [
      (request) => {
        const token = typeof window !== "undefined" ? localStorage.getItem("token") : null;
        if (token) {
          request.headers.set("Authorization", `Bearer ${token}`);
        }
      },
    ],
  },
});

export interface Project {
  id: string;
  user_id: string;
  title: string;
  description: string | null;
  language: string;
  created_at: string;
  updated_at: string;
  source_count: number;
  episode_count: number;
}

export interface Source {
  id: string;
  project_id: string;
  source_type: string;
  original_url: string | null;
  title: string | null;
  ingestion_status: string;
  is_temporary: boolean;
  metadata?: Record<string, unknown> | null;
  created_at: string;
}

export interface Episode {
  id: string;
  project_id: string;
  title: string;
  description: string | null;
  language: string;
  duration_seconds: number;
  audio_file_path: string | null;
  transcript_json: Array<{ start_ms: number; end_ms: number; speaker: string; text: string }> | null;
  shownotes_json: Record<string, unknown> | null;
  quality_metrics: Record<string, number> | null;
  created_at: string;
}

export interface GenerationRun {
  id: string;
  project_id: string;
  user_id: string;
  show_bible_id: string;
  status: string;
  config: Record<string, unknown>;
  current_phase: string | null;
  current_step: string | null;
  progress_pct: number | null;
  estimated_cost: { estimated_llm_cost: number; estimated_tts_cost: number; estimated_total: number } | null;
  actual_cost: Record<string, unknown> | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export type GenerationMode = "v1" | "v2" | "compare";
export type ExecutionMode = "full" | "step";

export interface EpisodePlanActArtifact {
  act_number: number;
  story_role: string | null;
  act_title: string | null;
  purpose: string | null;
  internal_arc: string | null;
  opening_question: string | null;
  ending_turn: string | null;
}

export interface BeatSheetSceneArtifact {
  scene_number: number;
  scene_type: string | null;
  scene_job: string | null;
  description: string | null;
  transition_note: string | null;
}

export interface BeatSheetActArtifact {
  act_number: number;
  story_role: string | null;
  act_title: string | null;
  scenes: BeatSheetSceneArtifact[];
}

export interface ScreenplayLineArtifact {
  speaker: string;
  text: string;
}

export interface ScreenplaySceneArtifact {
  scene_number: number;
  dialogue: ScreenplayLineArtifact[];
}

export interface ScreenplayActArtifact {
  act_number: number;
  scenes: ScreenplaySceneArtifact[];
}

export interface GenerationArtifacts {
  run_id: string;
  episode_plan: EpisodePlanActArtifact[];
  beat_sheets: BeatSheetActArtifact[];
  screenplay: ScreenplayActArtifact[];
}

export interface V2ArtifactItem {
  id: string;
  artifact_key: string;
  version: number;
  attempt: number;
  is_current: boolean;
  prompt_hash: string | null;
  input_artifact_ids: string[];
  superseded_by_id: string | null;
  status: string;
  provider: string | null;
  model: string | null;
  created_at: string | null;
  payload: Record<string, unknown>;
}

export interface V2Artifacts {
  run_id: string;
  artifacts: Record<string, V2ArtifactItem[]>;
  compare_v1_run_id: string | null;
  compare_v2_run_id: string | null;
  compare_v1_artifacts: GenerationArtifacts | null;
  compare_v2_artifacts: Record<string, V2ArtifactItem[]> | null;
}

export interface V2ToolTrace {
  id: string;
  phase: string;
  agent_name: string;
  tool_name: string;
  input_json: Record<string, unknown>;
  output_json: Record<string, unknown>;
  provider_call_id: string | null;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  cost_basis: string | null;
  cost_warning: string | null;
  created_at: string | null;
}

export interface V2ToolTraces {
  run_id: string;
  traces: V2ToolTrace[];
}

export interface V2EvidenceItem {
  id: string;
  generation_run_id: string;
  project_id: string;
  source_id: string;
  source_version_id: string;
  provider: string | null;
  provider_vector_store_id: string | null;
  provider_file_id: string | null;
  provider_vector_store_file_id: string | null;
  response_id: string | null;
  tool_call_id: string | null;
  retrieval_channel: string;
  query_text: string | null;
  purpose: string | null;
  search_result_index: number | null;
  score: number | null;
  snippet_text: string;
  normalized_quote_text: string | null;
  locator_text: string | null;
  locator_type: string;
  evidence_strength: string;
  snippet_hash: string;
  metadata: Record<string, unknown>;
  created_at: string | null;
}

export interface V2EvidenceResponse {
  run_id: string;
  evidence: V2EvidenceItem[];
  compare_v2_run_id: string | null;
  compare_v2_evidence: V2EvidenceItem[] | null;
}

export interface CostEstimate {
  estimated_llm_cost: number;
  estimated_tts_cost: number;
  estimated_total: number;
  estimated_minutes: number;
}
