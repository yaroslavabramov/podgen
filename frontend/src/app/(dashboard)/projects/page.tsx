"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useProjectStore } from "@/stores/project-store";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";

export default function ProjectsPage() {
  const { projects, loading, fetchProjects, createProject } = useProjectStore();
  const [showCreate, setShowCreate] = useState(false);
  const [title, setTitle] = useState("");
  const [language, setLanguage] = useState("en");

  useEffect(() => {
    fetchProjects();
  }, [fetchProjects]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    await createProject({ title, language });
    setTitle("");
    setShowCreate(false);
  };

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border px-6 py-4 flex items-center justify-between">
        <h1 className="text-xl font-bold text-primary">Podgen</h1>
        <Button onClick={() => setShowCreate(true)}>New Project</Button>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-8">
        <h2 className="text-2xl font-bold mb-6">Your Projects</h2>

        {showCreate && (
          <Card className="mb-6">
            <CardContent className="pt-6">
              <form onSubmit={handleCreate} className="flex gap-3">
                <Input
                  placeholder="Project title"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  className="flex-1"
                />
                <select
                  value={language}
                  onChange={(e) => setLanguage(e.target.value)}
                  className="rounded-md border border-border px-3 py-2 text-sm bg-background"
                >
                  <option value="en">English</option>
                  <option value="ru">Russian</option>
                </select>
                <Button type="submit">Create</Button>
                <Button type="button" variant="ghost" onClick={() => setShowCreate(false)}>Cancel</Button>
              </form>
            </CardContent>
          </Card>
        )}

        {loading ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : projects.length === 0 ? (
          <div className="text-center py-12">
            <p className="text-muted-foreground mb-4">No projects yet. Create your first one!</p>
            <Button onClick={() => setShowCreate(true)}>Create Project</Button>
          </div>
        ) : (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {projects.map((project) => (
              <Link key={project.id} href={`/projects/${project.id}`}>
                <Card className="hover:border-primary/50 transition-colors cursor-pointer">
                  <CardHeader>
                    <CardTitle className="text-lg">{project.title}</CardTitle>
                    <CardDescription>{project.description || "No description"}</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <div className="flex gap-2">
                      <Badge variant="secondary">{project.source_count} sources</Badge>
                      <Badge variant="secondary">{project.episode_count} episodes</Badge>
                      <Badge variant="outline">{project.language.toUpperCase()}</Badge>
                    </div>
                  </CardContent>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
