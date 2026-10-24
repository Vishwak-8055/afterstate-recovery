"use client";
import { useParams } from "next/navigation";
import type { ReactNode } from "react";
import { AppShell } from "@/components/shell";

export default function WorkspaceLayout({ children }: { children: ReactNode }) {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  return <AppShell workspaceId={workspaceId}>{children}</AppShell>;
}
