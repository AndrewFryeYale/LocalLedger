# PDF agenda fixtures

Everything here was trimmed from real public responses fetched 2026-10-17 with the declared User-Agent.

| File | Source | Trimming |
|---|---|---|
| `smithtown_events.json` | `https://smithtownny.api.civicclerk.com/v1/Events?$filter=startDateTime ge 2026-08-01T00:00:00Z and startDateTime le 2026-10-31T00:00:00Z&$orderby=startDateTime desc` | 3 of 15 event objects kept (4451 Board of Zoning Appeals, 4463 Planning Board, 4237 Town Board); `@odata.nextLink` removed |
| `smithtown_pb_2026-10-07.pdf` | `.../v1/Meetings/GetMeetingFileStream(fileId=11582,plainText=false)` (Planning Board decision agenda) | none (1 page; item "#1178 Gyrodyne LLC, St. James") |
| `smithtown_tb_2026-10-06_p1.txt` | pdfplumber text of page 1 of fileId=11566 (Town Board agenda) | text only: the PDF page with its embedded fonts was 160 KB. Contains "St. James Halloween Parade" (a false positive) |
| `cortlandt_meetings.html` | `https://www.townofcortlandtny.gov/cn/meetings/` | 6 `<tr>` rows kept verbatim (Oct/Sep/Jun meetings), wrapped in a minimal page |
| `cortlandt_tb_2026-10-20_p2.txt` | pdfplumber text of page 2 of `/uppages/AGENDA-OCT26-FOR POSTING.pdf` (9.7 MB, 91 pages) | text only; the Town Clerk letterhead "Cortlandt Manor, NY 10567" is a false positive for GYRO |
| `asburypark_pb_index.html` | `https://www.cityofasburypark.com/AgendaCenter/Planning-Board-9` | 8 `<tr>` rows kept verbatim, wrapped in a minimal page |
| `asburypark_pb_2026-08-03.pdf` | `/AgendaCenter/ViewFile/Agenda/_08032026-1313` | none (1 page; "APW Redeveloper LLC & Asbury Partners LLC") |
| `asburypark_cc_2026-10-14_p2.pdf` | `https://asburyparknj.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=5749,plainText=false)` | page 2 of 3 (pypdf); ordinance vacating part of St. James Place "Pursuant To ... The City's Waterfront Redevelopment Plan" |
| `labos_index.html` | `https://bos.lacounty.gov/board-meeting-agendas/` | 6 agenda `<a>` elements kept verbatim, wrapped in a list |
