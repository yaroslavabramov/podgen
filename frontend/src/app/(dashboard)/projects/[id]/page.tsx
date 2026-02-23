"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { useProjectStore } from "@/stores/project-store";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { SourceUploader } from "@/components/source-uploader";
import { ShowBibleEditor } from "@/components/show-bible-editor";

export default function ProjectDetailPage() {
  const params = useParams();
  const id = params.id as string;
  const { currentProject, sources, fetchProject, fetchSources } = useProjectStore();
  const [activeTab, setActiveTab] = useState<"sources" | "bible" | "episodes">("sources");

  useEffect(() => {
    fetchProject(id);
    fetchSources(id);
  }, [id, fetchProject, fetchSources]);

  if (!currentProject) return <div className="p-8 text-muted-foreground">Loading...</div>;

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link href="/projects" className="text-muted-foreground hover:text-foreground">Projects</Link>
          <span className="text-muted-foreground">/</span>
          <h1 className="text-xl font-bold">{currentProject.title}</h1>
        </div>
        <Link href={`/projects/${id}/generate`}>
          <Button>Generate Podcast</Button>
        </Link>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-8">
        <div className="flex gap-1 mb-6 border-b border-border">
          {(["sources", "bible", "episodes"] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                activeTab === tab
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              {tab === "sources" ? "Sources" : tab === "bible" ? "Show Bible" : "Episodes"}
            </button>
          ))}
        </div>

        {activeTab === "sources" && (
          <div className="space-y-6">
            <SourceUploader projectId={id} onUploaded={() => fetchSources(id)} />

            <div className="space-y-3">
              {sources.map((source) => (
                <Card key={source.id}>
                  <CardContent className="flex items-center justify-between py-4">
                    <div>
                      <p className="font-medium">{source.title || source.original_url || "Untitled"}</p>
                      <p className="text-sm text-muted-foreground">{source.source_type.toUpperCase()}</p>
                    </div>
                    <Badge
                      variant={
                        source.ingestion_status === "completed" ? "success" :
                        source.ingestion_status === "failed" ? "destructive" :
                        source.ingestion_status === "processing" ? "warning" : "secondary"
                      }
                    >
                      {source.ingestion_status}
                    </Badge>
                  </CardContent>
                </Card>
              ))}
              {sources.length === 0 && (
                <p className="text-center text-muted-foreground py-8">No sources yet. Upload files or add URLs above.</p>
              )}
            </div>
          </div>
        )}

        {activeTab === "bible" && <ShowBibleEditor projectId={id} />}

        {activeTab === "episodes" && (
          <div className="space-y-3">
            <p className="text-muted-foreground">Episodes will appear here after generation.</p>
          </div>
        )}
      </main>
    </div>
  );
}
