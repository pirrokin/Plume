export type Align = "left" | "center" | "right" | "justify";

export interface PageInfo {
  index: number;
  width: number;
  height: number;
  rotation: number;
  unrotated: [number, number];
  matrix: [number, number, number, number, number, number];
  rev: string;
}

export interface DocInfo {
  id: string;
  name: string;
  version: number;
  canUndo: boolean;
  canRedo: boolean;
  pages: PageInfo[];
  scanned: number[];
}

export interface Style {
  family: string;
  weight: number;
  italic: boolean;
  size: number;
  color: string;
  alpha: number;
  underline: boolean;
  strike: boolean;
  rise: number;
  spacing: number;
  render: string;
}

export interface Run extends Style {
  text: string;
}

export interface Line {
  bbox: [number, number, number, number];
  baseline: number;
  text: string;
  runs: Run[];
}

export interface Block {
  id: string;
  sig: string;
  bbox: [number, number, number, number];
  x: number;
  width: number;
  baseline: number;
  lineHeight: number;
  align: Align;
  indent: number;
  wrap: boolean;
  size: number;
  lines: Line[];
  runs: Run[];
  editable: boolean;
  issues: string[];
  scan?: boolean;
  bg?: string;
}

export interface FamilyInfo {
  key: string;
  name: string;
  generic: string;
  available?: boolean;
  substituted?: boolean;
  bold?: boolean;
  italic?: boolean;
  popular?: boolean;
  substitute?: string | null;
}

export interface PageModel {
  page: number;
  version: number;
  rev: string;
  blocks: Block[];
  others: { bbox: number[]; text: string; reason: string }[];
  families: FamilyInfo[];
}

export interface Box {
  x: number;
  y: number;
  width: number;
  wrap: boolean;
  align: Align;
  lineHeight: number;
  indent: number;
  size: number;
}

export interface EditReport {
  keptLines: number;
  rewrittenLines: number;
  removedChars: number;
  restoredChars: number;
  bbox: number[] | null;
}

export interface EditResult {
  info: DocInfo;
  page: PageModel;
  report: EditReport;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    throw new ApiError("Le serveur ne répond pas. Est-il démarré ?", 0);
  }
  if (!res.ok) {
    let message = `Erreur ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = "Requête invalide";
    } catch {
      /* not JSON */
    }
    throw new ApiError(message, res.status);
  }
  return (await res.json()) as T;
}

function json(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

const base = (id: string) => `/api/documents/${encodeURIComponent(id)}`;

export const api = {
  upload(file: File): Promise<DocInfo> {
    const form = new FormData();
    form.append("file", file, file.name);
    return request<DocInfo>("/api/documents", { method: "POST", body: form });
  },
  info: (id: string) => request<DocInfo>(base(id)),
  page: (id: string, page: number) => request<PageModel>(`${base(id)}/pages/${page}/text`),
  edit: (id: string, page: number, blockId: string, body: { version: number; sig: string; runs: Run[]; box: Box }) =>
    request<EditResult>(`${base(id)}/pages/${page}/blocks/${encodeURIComponent(blockId)}`, json("POST", body)),
  remove: (id: string, page: number, blockId: string, version: number, sig: string) =>
    request<EditResult>(
      `${base(id)}/pages/${page}/blocks/${encodeURIComponent(blockId)}?version=${version}&sig=${encodeURIComponent(sig)}`,
      { method: "DELETE" },
    ),
  add: (id: string, page: number, body: { version: number; runs: Run[]; box: Box }) =>
    request<EditResult>(`${base(id)}/pages/${page}/blocks`, json("POST", body)),
  ocr: (id: string, page: number, version: number) =>
    request<{ info: DocInfo; page: PageModel; words: number }>(`${base(id)}/pages/${page}/ocr`, json("POST", { version })),
  pageOp: (id: string, page: number, body: { version: number; op: string; angle?: number; to?: number }) =>
    request<DocInfo>(`${base(id)}/pages/${page}/op`, json("POST", body)),
  merge(id: string, file: File, version: number): Promise<DocInfo> {
    const form = new FormData();
    form.append("file", file, file.name);
    return request<DocInfo>(`${base(id)}/merge?version=${version}`, { method: "POST", body: form });
  },
  undo: (id: string) => request<DocInfo>(`${base(id)}/undo`, { method: "POST" }),
  redo: (id: string) => request<DocInfo>(`${base(id)}/redo`, { method: "POST" }),
  fonts: () => request<FamilyInfo[]>("/api/fonts"),
  imageUrl(id: string, page: number, scale: number, rev: string, hide?: string): string {
    const q = new URLSearchParams({ scale: scale.toFixed(2), rev });
    if (hide) q.set("hide", hide);
    return `${base(id)}/pages/${page}/image?${q}`;
  },
  downloadUrl: (id: string) => `${base(id)}/download`,
  fontsCssUrl: (id: string, families: string[]) =>
    `${base(id)}/fonts.css?families=${encodeURIComponent(families.join(","))}`,
};
