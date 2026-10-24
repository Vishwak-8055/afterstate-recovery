"use client";
import { useParams } from "next/navigation";
import { RunWorkspace } from "@/components/run-workspace";

export default function RunPage() {
  const { id } = useParams<{ id: string }>();
  return <RunWorkspace runId={id} />;
}
