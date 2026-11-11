import os
import re
import socket
import ipaddress
import tempfile
import zipfile
import html
from typing import Any
from urllib.parse import urlparse
import httpx
from bs4 import BeautifulSoup
import trafilatura
from pypdf import PdfReader
from aiogram import Bot

MAX_WEB_SIZE = 3 * 1024 * 1024  # 3 MB
MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB
MAX_CONTEXT_CHARS = 15_000
HTTP_TIMEOUT = 12.0
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


class SandboxInspector:
    """Isolated security sandbox for web inspection, GitHub analysis, and file parsing."""

    @staticmethod
    def is_safe_url(url: str) -> bool:
        """SSRF protection: verify URL scheme and ensure IP does not resolve to private/local nets."""
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() not in ("http", "https"):
                return False

            hostname = parsed.hostname
            if not hostname:
                return False

            # Resolve hostname to all addresses
            addr_info = socket.getaddrinfo(hostname, None)
            for item in addr_info:
                ip_str = item[4][0]
                ip = ipaddress.ip_address(ip_str)

                # Check IPv4 mapped into IPv6
                if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                    ip = ip.ipv4_mapped

                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_multicast
                    or ip.is_unspecified
                ):
                    return False

            return True
        except (socket.gaierror, ValueError, Exception):
            return False

    @staticmethod
    def parse_github_repo(url: str) -> tuple[str, str] | None:
        """Extract owner and repo from a GitHub URL."""
        match = re.search(r"github\.com/([^/]+)/([^/#?]+)", url)
        if match:
            owner = match.group(1).strip()
            repo = match.group(2).strip().removesuffix(".git")
            return owner, repo
        return None

    async def inspect_github(self, url: str) -> dict[str, Any]:
        """Deep technical inspection of a GitHub repository via public API and raw files."""
        parsed_repo = self.parse_github_repo(url)
        if not parsed_repo:
            return await self.inspect_url(url)

        owner, repo = parsed_repo
        headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "application/vnd.github.v3+json",
        }

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
            # 1. Fetch Repo Metadata
            repo_api_url = f"https://api.github.com/repos/{owner}/{repo}"
            repo_res = await client.get(repo_api_url)
            if repo_res.status_code != 200:
                # If API fails or is rate limited, fallback to standard webpage scraper
                return await self.inspect_url(url)

            meta = repo_res.json()
            full_name = meta.get("full_name", f"{owner}/{repo}")
            description = meta.get("description", "") or "No description provided."
            stars = meta.get("stargazers_count", 0)
            forks = meta.get("forks_count", 0)
            language = meta.get("language", "Unknown")
            topics = meta.get("topics", [])
            default_branch = meta.get("default_branch", "main")
            license_name = (meta.get("license") or {}).get("name", "Not specified")

            # 2. Fetch File Tree (first 80 files)
            tree_api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{default_branch}?recursive=1"
            tree_res = await client.get(tree_api_url)
            file_paths: list[str] = []
            if tree_res.status_code == 200:
                tree_data = tree_res.json()
                items = tree_data.get("tree", [])
                file_paths = [
                    item["path"]
                    for item in items
                    if item.get("type") == "blob"
                ][:80]

            # 3. Fetch README excerpt
            readme_text = ""
            readme_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/README.md"
            readme_res = await client.get(readme_url)
            if readme_res.status_code == 200:
                readme_text = readme_res.text[:8000]
            else:
                # Try lowercase
                readme_url_lower = f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/readme.md"
                r_lower = await client.get(readme_url_lower)
                if r_lower.status_code == 200:
                    readme_text = r_lower.text[:8000]

            # 4. Fetch Key Manifests if present
            manifest_candidates = [
                "pyproject.toml",
                "package.json",
                "Cargo.toml",
                "go.mod",
                "requirements.txt",
            ]
            manifest_contents: dict[str, str] = {}
            for manifest_name in manifest_candidates:
                if manifest_name in file_paths:
                    m_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/{manifest_name}"
                    m_res = await client.get(m_url)
                    if m_res.status_code == 200:
                        manifest_contents[manifest_name] = m_res.text[:2000]
                if len(manifest_contents) >= 2:
                    break

        # Assemble technical passport
        passport_lines = [
            f"GitHub Repository: {full_name}",
            f"URL: https://github.com/{owner}/{repo}",
            f"Stars: {stars} | Forks: {forks} | Language: {language} | License: {license_name}",
            f"Topics: {', '.join(topics) if topics else 'None'}",
            f"Description: {description}\n",
            "--- File Structure Sample (Top 80 items) ---",
            "\n".join(file_paths[:40]) if file_paths else "Tree not accessible",
            "\n--- Manifests & Configs ---",
        ]
        for m_name, m_body in manifest_contents.items():
            passport_lines.append(f"[{m_name}]\n{m_body}\n")

        if readme_text:
            passport_lines.append(f"--- README Excerpt ---\n{readme_text}")

        raw_context = "\n".join(passport_lines)[:MAX_CONTEXT_CHARS]
        title = f"{owner}/{repo}: {description[:80]}" if description else full_name

        return {
            "type": "github",
            "title": title,
            "url": f"https://github.com/{owner}/{repo}",
            "stars": stars,
            "language": language,
            "description": description,
            "topics": topics,
            "raw_context": raw_context,
        }

    async def inspect_url(self, url: str) -> dict[str, Any]:
        """Universal web scraper with SSRF protection, Trafilatura and BeautifulSoup fallback."""
        if not self.is_safe_url(url):
            return {
                "type": "blocked",
                "title": "Blocked URL",
                "url": url,
                "raw_context": f"URL {url} failed SSRF security validation (private or link-local address).",
            }

        headers = {"User-Agent": BROWSER_USER_AGENT}
        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                headers=headers,
                follow_redirects=True,
            ) as client:
                res = await client.get(url)
                if res.status_code >= 400:
                    return {
                        "type": "error",
                        "title": f"HTTP {res.status_code}",
                        "url": url,
                        "raw_context": f"Failed to retrieve {url}: HTTP {res.status_code}",
                    }

                # Verify final redirect URL against SSRF
                if not self.is_safe_url(str(res.url)):
                    return {
                        "type": "blocked",
                        "title": "Blocked Redirect",
                        "url": str(res.url),
                        "raw_context": "Redirected URL failed SSRF security validation.",
                    }

                # Limit downloaded payload size
                content_bytes = res.content[:MAX_WEB_SIZE]
                html_text = content_bytes.decode(res.encoding or "utf-8", errors="replace")

            # 1. Primary extractor: Trafilatura
            extracted_text = trafilatura.extract(
                html_text,
                include_links=True,
                include_formatting=True,
                url=url,
            )

            soup = BeautifulSoup(html_text, "html.parser")
            page_title = ""
            if soup.title and soup.title.string:
                page_title = soup.title.string.strip()

            meta_desc = ""
            desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find(
                "meta", attrs={"property": "og:description"}
            )
            if desc_tag and desc_tag.get("content"):
                meta_desc = str(desc_tag.get("content")).strip()

            if not extracted_text:
                # Fallback to BeautifulSoup body text
                body = soup.body
                if body:
                    for s in body(["script", "style", "nav", "footer", "header"]):
                        s.extract()
                    extracted_text = body.get_text(separator="\n", strip=True)
                else:
                    extracted_text = meta_desc or "No readable text content."

            full_text = f"Title: {page_title}\nDescription: {meta_desc}\n\nContent:\n{extracted_text}"
            raw_context = full_text[:MAX_CONTEXT_CHARS]
            title = page_title or (meta_desc[:80] if meta_desc else url)

            return {
                "type": "web",
                "title": title,
                "url": url,
                "raw_context": raw_context,
            }
        except Exception as e:
            return {
                "type": "error",
                "title": "Inspection Error",
                "url": url,
                "raw_context": f"Error inspecting {url}: {str(e)}",
            }

    async def inspect_telegram_file(
        self, bot: Bot, file_id: str, file_name: str
    ) -> dict[str, Any]:
        """Download and inspect files in an isolated temporary directory (ZIP/PDF/Code)."""
        file_info = await bot.get_file(file_id)
        if file_info.file_size and file_info.file_size > MAX_FILE_SIZE:
            return {
                "type": "file_error",
                "title": file_name,
                "raw_context": f"File {file_name} exceeds the maximum allowed size of 15MB.",
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            safe_name = os.path.basename(file_name or "file.bin")
            file_path = os.path.join(temp_dir, safe_name)

            if not file_info.file_path:
                return {
                    "type": "file_error",
                    "title": file_name,
                    "raw_context": "Could not locate file path on Telegram servers.",
                }

            await bot.download_file(file_info.file_path, file_path)

            ext = os.path.splitext(safe_name)[1].lower()
            context_chunks: list[str] = [f"File: {safe_name} ({ext})"]

            # Case A: ZIP archive with zip-bomb protection
            if ext == ".zip":
                try:
                    with zipfile.ZipFile(file_path, "r") as zf:
                        infolist = zf.infolist()
                        total_uncompressed = sum(info.file_size for info in infolist)
                        total_compressed = sum(info.compress_size for info in infolist) or 1

                        if len(infolist) > 500:
                            return {
                                "type": "file_error",
                                "title": safe_name,
                                "raw_context": "Rejected ZIP: Contains more than 500 files.",
                            }

                        if total_uncompressed > 50 * 1024 * 1024:
                            return {
                                "type": "file_error",
                                "title": safe_name,
                                "raw_context": "Rejected ZIP: Uncompressed content exceeds 50MB.",
                            }

                        if total_uncompressed / total_compressed > 100:
                            return {
                                "type": "file_error",
                                "title": safe_name,
                                "raw_context": "Rejected ZIP: Compression ratio exceeds 100:1 (potential zip-bomb).",
                            }

                        file_names = [info.filename for info in infolist][:100]
                        context_chunks.append("--- Archive File List (First 100 files) ---")
                        context_chunks.append("\n".join(file_names))

                        # Look for README and config files to read
                        for info in infolist:
                            base = os.path.basename(info.filename).lower()
                            if base.startswith("readme") or base in (
                                "package.json",
                                "pyproject.toml",
                                "cargo.toml",
                                "requirements.txt",
                            ):
                                try:
                                    with zf.open(info) as f:
                                        content = f.read(10_000).decode("utf-8", errors="replace")
                                        context_chunks.append(f"\n--- {info.filename} ---\n{content}")
                                except Exception:
                                    continue
                except Exception as e:
                    context_chunks.append(f"Failed to read ZIP archive: {str(e)}")

            # Case B: PDF document
            elif ext == ".pdf":
                try:
                    reader = PdfReader(file_path)
                    num_pages = len(reader.pages)
                    context_chunks.append(f"PDF Document: {num_pages} pages")
                    extracted_pages: list[str] = []
                    for idx, page in enumerate(reader.pages[:15]):
                        p_text = page.extract_text() or ""
                        if p_text.strip():
                            extracted_pages.append(f"[Page {idx + 1}]\n{p_text}")
                    context_chunks.append("\n\n".join(extracted_pages))
                except Exception as e:
                    context_chunks.append(f"Failed to extract PDF text: {str(e)}")

            # Case C: Text / Code files
            else:
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read(20_000)
                    context_chunks.append(f"--- File Contents ---\n{text}")
                except Exception as e:
                    context_chunks.append(f"Could not read text file: {str(e)}")

            raw_context = "\n\n".join(context_chunks)[:MAX_CONTEXT_CHARS]
            return {
                "type": "file",
                "title": safe_name,
                "url": "",
                "raw_context": raw_context,
            }


sandbox = SandboxInspector()
