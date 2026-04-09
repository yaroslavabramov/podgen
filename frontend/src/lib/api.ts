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

export interface CostEstimate {
  estimated_llm_cost: number;
  estimated_tts_cost: number;
  estimated_total: number;
  estimated_minutes: number;
}
