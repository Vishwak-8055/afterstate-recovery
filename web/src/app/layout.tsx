import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Providers } from "@/components/shell";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "AFTERSTATE: prove recovery before it matters", template: "%s | AFTERSTATE" },
  description: "Recovery verification for PostgreSQL: restore real backups into disposable environments, verify the data, measure RTO and RPO, and keep the evidence.",
};

const themeScript = `try{var t=localStorage.getItem("afterstate-theme");if(t)document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: themeScript }} /></head>
      <body><Providers>{children}</Providers></body>
    </html>
  );
}
