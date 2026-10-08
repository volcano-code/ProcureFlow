import type {QuoteValues,TableImportSelection,TableImportSheet} from './api';
export const TABLE_FIELDS: [keyof QuoteValues,string][];
export interface ImportTicket {epoch:number;signal:AbortSignal}
export interface ImportScope {begin():ImportTicket|null;current(ticket:ImportTicket|null):boolean;finish(ticket:ImportTicket):void;dispose():void}
export function createImportScope():ImportScope;
export function importReadOnly(status:string):boolean;
export function importExpired(expiresAt:string,now?:number):boolean;
export function compactMapping(mapping:Partial<Record<keyof QuoteValues,string|null>>):Partial<Record<keyof QuoteValues,string>>;
export function mappingProblems(selection:TableImportSelection,sheet:TableImportSheet|undefined):string[];
export function selectionForSheet(sheet:TableImportSheet):TableImportSelection;
export function previewBody(revision:number,selection:TableImportSelection):TableImportSelection&{expected_revision:number};
