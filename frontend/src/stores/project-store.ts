"use client";

import { create } from "zustand";
import { api, type Project, type Source } from "@/lib/api";

interface ProjectStore {
  projects: Project[];
  currentProject: Project | null;
  sources: Source[];
  loading: boolean;
  fetchProjects: () => Promise<void>;
  fetchProject: (id: string) => Promise<void>;
  fetchSources: (projectId: string) => Promise<void>;
  createProject: (data: { title: string; description?: string; language: string }) => Promise<Project>;
  updateProjectLanguage: (id: string, language: "en" | "ru") => Promise<Project>;
}

export const useProjectStore = create<ProjectStore>((set) => ({
  projects: [],
  currentProject: null,
  sources: [],
  loading: false,

  fetchProjects: async () => {
    set({ loading: true });
    try {
      const data = await api.get("api/projects").json<{ items: Project[] }>();
      set({ projects: data.items });
    } finally {
      set({ loading: false });
    }
  },

  fetchProject: async (id) => {
    set({ loading: true });
    try {
      const project = await api.get(`api/projects/${id}`).json<Project>();
      set({ currentProject: project });
    } finally {
      set({ loading: false });
    }
  },

  fetchSources: async (projectId) => {
    const data = await api.get(`api/projects/${projectId}/sources`).json<{ items: Source[] }>();
    set({ sources: data.items });
  },

  createProject: async (data) => {
    const project = await api.post("api/projects", { json: data }).json<Project>();
    set((state) => ({ projects: [project, ...state.projects] }));
    return project;
  },

  updateProjectLanguage: async (id, language) => {
    const project = await api.patch(`api/projects/${id}`, { json: { language } }).json<Project>();
    set((state) => ({
      currentProject: state.currentProject?.id === id ? project : state.currentProject,
      projects: state.projects.map((item) => (item.id === id ? project : item)),
    }));
    return project;
  },
}));
