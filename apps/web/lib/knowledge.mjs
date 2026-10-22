/** Evidence binding only. Browser metadata never grants publication authority. */
export function knowledgeBinding(snapshot, citationId) {
  const corpus = snapshot?.knowledge;
  if (!corpus || typeof corpus.corpus_hash !== "string" || !/^[a-f0-9]{64}$/.test(corpus.corpus_hash)
      || !Array.isArray(corpus.documents) || typeof citationId !== "string") return null;
  for (const document of corpus.documents) {
    if (!Array.isArray(document.chunks)) continue;
    const chunk = document.chunks.find(item => item?.id === citationId);
    if (chunk && typeof document.content_hash === "string" && typeof chunk.text === "string") {
      return {corpusHash: corpus.corpus_hash, scope: corpus.scope || "procurement",
        documentHash: document.content_hash, text: chunk.text, versionId: document.id};
    }
  }
  return null;
}
export function citationPath(id, hash, scope = "procurement") {
  return `/knowledge/citations/${encodeURIComponent(id)}?${new URLSearchParams({corpus_hash:hash,scope})}`;
}
export function latestDocumentVersion(versions, key) {
  return versions.filter(item => item.document_key === key).reduce((max, item) => Math.max(max, item.latest_version), 0);
}
