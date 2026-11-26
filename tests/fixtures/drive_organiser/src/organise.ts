// Drive organiser: Claude reads file names and snippets, then files things into folders.
import { google, drive_v3 } from "googleapis";
import Anthropic from "@anthropic-ai/sdk";

const anthropic = new Anthropic();
const auth = new google.auth.GoogleAuth({ scopes: ["https://www.googleapis.com/auth/drive"] });
const drive: drive_v3.Drive = google.drive({ version: "v3", auth });

export async function listLoose() {
  const res = await drive.files.list({
    q: "'root' in parents and trashed = false",
    fields: "files(id, name, mimeType, parents)",
    pageSize: 200,
  });
  return res.data.files ?? [];
}

export async function ensureFolder(name: string) {
  const res = await drive.files.create({
    requestBody: { name, mimeType: "application/vnd.google-apps.folder" },
    fields: "id",
  });
  return res.data.id!;
}

export async function moveTo(fileId: string, folderId: string, oldParent: string) {
  await drive.files.update({
    fileId,
    addParents: folderId,
    removeParents: oldParent,
    fields: "id, parents",
  });
}

export async function rename(fileId: string, name: string) {
  await drive.files.update({ fileId, requestBody: { name } });
}

export async function binIt(fileId: string) {
  await drive.files.update({ fileId, requestBody: { trashed: true } });
}

export async function shareWithTeam(fileId: string, email: string) {
  await drive.permissions.create({
    fileId,
    requestBody: { type: "user", role: "reader", emailAddress: email },
    sendNotificationEmail: false,
  });
}

export async function whoCanSee(fileId: string) {
  return (await drive.permissions.list({ fileId })).data.permissions;
}

export async function textOf(fileId: string) {
  const res = await drive.files.export({ fileId, mimeType: "text/plain" });
  return String(res.data);
}

export async function duplicate(fileId: string) {
  return drive.files.copy({ fileId });
}

export async function sinceLastRun(pageToken: string) {
  return drive.changes.list({ pageToken });
}

export async function purge(fileId: string) {
  await drive.files.delete({ fileId });
}

export async function decide(names: string[]) {
  const msg = await anthropic.messages.create({
    model: "claude-fable-5-2",
    max_tokens: 500,
    messages: [{ role: "user", content: "Sort these files into folders: " + names.join(", ") }],
  });
  return msg;
}
