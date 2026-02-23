"use client";


import { useEffect, useState } from "react";
import Link from "next/link";
import { useTranslations } from "use-intl";
import { useProjectStore } from "@/stores/project-store";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { LocaleSwitcher } from "@/components/locale-switcher";

export default function ProjectsPage() {
  const t = useTranslations("projectsPage");
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
        <div className="flex items-center gap-4">
          <h1 className="text-xl font-bold text-primary">Podgen</h1>
          <Link href="/episodes" className="text-sm text-muted-foreground hover:text-foreground">
            {t("episodesLink")}
          </Link>
        </div>
        <div className="flex items-center gap-3">
          <LocaleSwitcher />
          <Button onClick={() => setShowCreate(true)}>{t("newProject")}</Button>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-8">
        <h2 className="text-2xl font-bold mb-6">{t("title")}</h2>

        {showCreate && (
          <Card className="mb-6">
            <CardContent className="pt-6">
              <form onSubmit={handleCreate} className="flex gap-3">
                <Input
                  placeholder={t("projectTitlePlaceholder")}
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  className="flex-1"
                />
                <select
                  value={language}
                  onChange={(e) => setLanguage(e.target.value)}
                  className="rounded-md border border-border px-3 py-2 text-sm bg-background"
                >
                  <option value="en">{t("languageEnglish")}</option>
                  <option value="ru">{t("languageRussian")}</option>
                </select>
                <Button type="submit">{t("create")}</Button>
                <Button type="button" variant="ghost" onClick={() => setShowCreate(false)}>{t("cancel")}</Button>
              </form>
            </CardContent>
          </Card>
        )}

        {loading ? (
          <p className="text-muted-foreground">{t("loading")}</p>
        ) : projects.length === 0 ? (
          <div className="text-center py-12">
            <p className="text-muted-foreground mb-4">{t("noProjects")}</p>
            <Button onClick={() => setShowCreate(true)}>{t("createProject")}</Button>
          </div>
        ) : (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {projects.map((project) => (
              <Link key={project.id} href={`/projects/${project.id}`}>
                <Card className="hover:border-primary/50 transition-colors cursor-pointer">
                  <CardHeader>
                    <CardTitle className="text-lg">{project.title}</CardTitle>
                    <CardDescription>{project.description || t("noDescription")}</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <div className="flex gap-2">
                      <Badge variant="secondary">{t("sourcesCount", { count: project.source_count })}</Badge>
                      <Badge variant="secondary">{t("episodesCount", { count: project.episode_count })}</Badge>
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
