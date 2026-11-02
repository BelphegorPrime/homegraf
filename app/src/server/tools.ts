/**
 * Where a command-line tool is: in TOOLS_DIR (the copy in the image, where `docker exec homegraf homelab.py` finds it too), else in the
 * repository itself (development: `REPO_DIR=../seed npm run dev:server`, where the tools sit next to the data).
 */
export const toolPath = (root: string, file: string, env: NodeJS.ProcessEnv = process.env) => `${env.TOOLS_DIR || root}/${file}`;

/** What tells a tool which repository it works on (a read-only checkout, or the clone of a job). */
export const toolRoot = (root: string): Record<string, string> => ({ HOMELAB_ROOT: root });
