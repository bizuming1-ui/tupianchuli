import { integer, jsonb, pgTable, real, serial, text, timestamp } from "drizzle-orm/pg-core";

/** 审查历史（只保存结果与 160px 缩略图，不保存原图） */
export const scanRecords = pgTable("scan_records", {
  id: serial("id").primaryKey(),
  filename: text("filename").notNull(),
  verdict: text("verdict").notNull(), // pass | review | block
  pornScore: real("porn_score").notNull().default(0),
  pornLevel: text("porn_level").notNull().default("none"),
  politicalScore: real("political_score").notNull().default(0),
  politicalLevel: text("political_level").notNull().default("none"),
  description: text("description").notNull().default(""),
  reasons: jsonb("reasons").$type<string[]>().notNull().default([]),
  ocrText: text("ocr_text").notNull().default(""),
  engine: text("engine").notNull().default("demo"),
  elapsedMs: integer("elapsed_ms").notNull().default(0),
  thumb: text("thumb").notNull().default(""),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

export type ScanRecord = typeof scanRecords.$inferSelect;
