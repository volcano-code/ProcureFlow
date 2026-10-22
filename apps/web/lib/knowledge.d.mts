export function knowledgeBinding(snapshot:unknown,citationId:string):{corpusHash:string;scope:string;documentHash:string;text:string;versionId:string}|null;
export function citationPath(id:string,hash:string,scope?:string):string;
export function latestDocumentVersion(versions:{document_key:string;latest_version:number}[],key:string):number;
