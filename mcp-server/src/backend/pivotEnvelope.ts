import * as z from 'zod';

// Validate the transport contract, while leaving backend-owned business data
// and future status/action values extensible. Never coerce malformed success.
const object = z.record(z.string(), z.unknown());
export const pivotEnvelopeShape = {
  contract_version: z.string().min(1),
  migration_id: z.string().nullable().optional(),
  operation_id: z.string().nullable().optional(),
  status: z.string().min(1),
  next_action: z.string().min(1),
  retry_after_seconds: z.number().finite().nonnegative().optional(),
  progress: object.optional(),
  data: object,
  error: z.object({
    code: z.string(), message: z.string(), retryable: z.boolean(), next_action: z.string(),
    details: object.optional(),
  }).nullable(),
};
export const pivotEnvelopeSchema = z.object(pivotEnvelopeShape);
