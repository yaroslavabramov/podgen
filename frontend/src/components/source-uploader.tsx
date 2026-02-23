"use client";

import { useState, useCallback } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";

interface Props {
  projectId: string;
  onUploaded: () => void;
}

export function SourceUploader({ projectId, onUploaded }: Props) {
  const [url, setUrl] = useState("");
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);

  const handleFileUpload = async (files: FileList) => {
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        const formData = new FormData();
        formData.append("file", file);
        await fetch(
          `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/projects/${projectId}/sources/upload`,
          {
            method: "POST",
            headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
            body: formData,
          }
        );
      }
      onUploaded();
    } finally {
      setUploading(false);
    }
  };

  const handleUrlAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    setUploading(true);
    try {
      await api.post(`api/projects/${projectId}/sources/url`, { json: { url, source_type: "web" } });
      setUrl("");
      onUploaded();
    } finally {
      setUploading(false);
    }
  };

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files.length) handleFileUpload(e.dataTransfer.files);
  }, []);

  return (
    <div className="space-y-4">
      <Card
        className={`border-2 border-dashed transition-colors ${dragOver ? "border-primary bg-primary/5" : "border-border"}`}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
      >
        <CardContent className="py-8 text-center">
          <p className="text-muted-foreground mb-3">
            {uploading ? "Uploading..." : "Drag & drop files here, or click to browse"}
          </p>
          <input
            type="file"
            multiple
            accept=".pdf,.mp3,.wav,.m4a,.mp4,.webm"
            onChange={(e) => e.target.files && handleFileUpload(e.target.files)}
            className="hidden"
            id="file-upload"
          />
          <label htmlFor="file-upload">
            <Button variant="outline" asChild disabled={uploading}>
              <span>Browse Files</span>
            </Button>
          </label>
          <p className="text-xs text-muted-foreground mt-2">PDF, Audio, Video</p>
        </CardContent>
      </Card>

      <form onSubmit={handleUrlAdd} className="flex gap-3">
        <Input
          placeholder="Add URL (web page, article, video)"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          className="flex-1"
        />
        <Button type="submit" variant="outline" disabled={!url.trim() || uploading}>
          Add URL
        </Button>
      </form>
    </div>
  );
}
