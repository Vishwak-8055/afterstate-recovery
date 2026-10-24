import type { FieldValues, Resolver } from "react-hook-form";
import type { ZodType } from "zod";

/** Tiny Zod adapter for react-hook-form (keeps the dependency tree small and auditable). */
export function zodResolver<T extends FieldValues>(schema: ZodType<T>): Resolver<T> {
  return async (values) => {
    const r = schema.safeParse(values);
    if (r.success) return { values: r.data, errors: {} };
    const errors: Record<string, { type: string; message: string }> = {};
    for (const issue of r.error.issues) {
      const key = issue.path.join(".");
      if (!errors[key]) errors[key] = { type: issue.code, message: issue.message };
    }
    return { values: {}, errors } as never;
  };
}
