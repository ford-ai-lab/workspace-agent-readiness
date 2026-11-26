// Posts the booking to a Chat space and logs it to a sheet (fetch, no SDK).
export async function announce(token, space, text) {
  await fetch(`https://chat.googleapis.com/v1/spaces/${space}/messages`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

export async function logRow(token, sheetId, row) {
  const range = encodeURIComponent("Log!A1");
  await fetch(`https://sheets.googleapis.com/v4/spreadsheets/${sheetId}/values/${range}:append?valueInputOption=RAW`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: JSON.stringify({ values: [row] }),
  });
}

export async function addTodo(token, list, title) {
  return fetch("https://tasks.googleapis.com/tasks/v1/lists/" + list + "/tasks", { method: "POST", body: JSON.stringify({ title }) });
}
