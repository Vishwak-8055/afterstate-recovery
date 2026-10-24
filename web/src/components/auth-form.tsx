"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { describeError, post } from "@/lib/api";
import { zodResolver } from "@/lib/zod-resolver";
import { Logo, useAuth } from "./shell";
import { Button, Field, Input } from "./ui";

const login = z.object({ email: z.string().email("Enter a valid email address."), password: z.string().min(1, "Enter your password.") });
const register = login.extend({ display_name: z.string().min(1, "Enter your name.").max(120), password: z.string().min(12, "Use at least 12 characters.") });

function Form({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const next = useSearchParams().get("next");
  const { refresh } = useAuth();
  const [error, setError] = useState("");
  const schema = mode === "login" ? login : register;
  const { register: reg, handleSubmit, formState } = useForm<{ email: string; password: string; display_name?: string }>({ resolver: zodResolver(schema as never) });
  const submit = handleSubmit(async (v) => {
    setError("");
    try {
      const me = await post<{ memberships: { workspace_id: string }[] }>(`/auth/${mode}`, v);
      refresh();
      router.push(next && next.startsWith("/") && !next.startsWith("//") ? next : me.memberships[0] ? `/w/${me.memberships[0].workspace_id}/overview` : "/onboarding");
    } catch (e) { setError(describeError(e)); }
  });
  return (
    <div className="grid min-h-dvh md:grid-cols-[1fr_28rem]">
      <div className="hidden flex-col justify-between border-r border-line bg-s1 p-10 md:flex">
        <Logo />
        <div><p className="max-w-md text-2xl font-semibold tracking-tight">Recovery is an engineering capability that must be tested, measured, and evidenced.</p><p className="mt-3 max-w-md text-sm text-muted">Restore real backups into disposable environments and keep the proof.</p></div>
        <p className="text-xs text-muted">PostgreSQL logical backups. No claims beyond what a run measures.</p>
      </div>
      <main className="grid place-items-center p-6">
        <form onSubmit={submit} noValidate className="w-full max-w-sm space-y-4">
          <h1 className="text-2xl font-semibold tracking-tight">{mode === "login" ? "Sign in" : "Create your account"}</h1>
          {mode === "register" && <Field label="Name" error={formState.errors.display_name?.message}>{(id) => <Input id={id} autoComplete="name" {...reg("display_name")} />}</Field>}
          <Field label="Email" error={formState.errors.email?.message}>{(id) => <Input id={id} type="email" autoComplete="email" {...reg("email")} />}</Field>
          <Field label="Password" hint={mode === "register" ? "At least 12 characters." : undefined} error={formState.errors.password?.message}>{(id) => <Input id={id} type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} {...reg("password")} />}</Field>
          {error && <p role="alert" className="rounded-md border border-fail-line bg-fail-bg px-3 py-2 text-sm text-fail">{error}</p>}
          <Button type="submit" variant="primary" busy={formState.isSubmitting} className="w-full">{mode === "login" ? "Sign in" : "Create account"}</Button>
          <p className="text-sm text-muted">{mode === "login" ? <>New here? <Link className="text-ink underline" href="/register">Create an account</Link></> : <>Already registered? <Link className="text-ink underline" href="/login">Sign in</Link></>}</p>
        </form>
      </main>
    </div>
  );
}
export const AuthForm = ({ mode }: { mode: "login" | "register" }) => <Suspense><Form mode={mode} /></Suspense>;
