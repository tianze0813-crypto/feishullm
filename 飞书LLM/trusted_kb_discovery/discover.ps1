[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OpenId,

    [string]$QueriesFile = (Join-Path $PSScriptRoot "queries_full.json"),
    [string]$TokenStore = (Join-Path (Split-Path $PSScriptRoot -Parent) ".tokens\user_tokens.json"),
    [string]$OutputDir = (Join-Path $PSScriptRoot "output"),
    [string]$FeishuBaseUrl = "https://open.feishu.cn",
    [string]$FeishuWebBaseUrl = "https://www.feishu.cn",
    [int]$DocsPageSize = 8,
    [int]$WikiPageSize = 8,
    [int]$RawTopN = 120,
    [int]$TopN = 100,
    [int]$BatchSize = 0,
    [int]$BatchIndex = 1,
    [int]$QuerySleepMs = 0
)

$ErrorActionPreference = "Stop"

$SignalsFile = Join-Path $PSScriptRoot "signals.json"
$Signals = Get-Content -LiteralPath $SignalsFile -Raw -Encoding UTF8 | ConvertFrom-Json
$TitlePositiveSignals = @($Signals.title_positive)
$TitleNegativeSignals = @($Signals.title_negative)
$ContentPositiveSignals = @($Signals.content_positive)

function Get-FirstValue {
    param(
        [object]$Item,
        [string[]]$Keys,
        [string]$DefaultValue = ""
    )
    foreach ($key in $Keys) {
        if ($Item -is [hashtable]) {
            if ($Item.ContainsKey($key)) {
                $value = [string]$Item[$key]
                if ($value) { return $value }
            }
        } else {
            $prop = $Item.PSObject.Properties[$key]
            if ($prop) {
                $value = [string]$prop.Value
                if ($value) { return $value }
            }
        }
    }
    return $DefaultValue
}

function Get-UserToken {
    param([string]$StorePath, [string]$TargetOpenId)
    $raw = Get-Content -LiteralPath $StorePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $prop = $raw.PSObject.Properties[$TargetOpenId]
    if (-not $prop) {
        throw "open_id=$TargetOpenId not found in token store"
    }
    $record = $prop.Value
    $token = [string]$record.access_token
    if (-not $token) {
        throw "open_id=$TargetOpenId has empty access_token"
    }
    return $token
}

function Invoke-FeishuJson {
    param(
        [string]$Method,
        [string]$Url,
        [string]$AccessToken,
        [hashtable]$Body = $null
    )
    $args = @(
        "--silent",
        "--show-error",
        "--location",
        "--noproxy", "*",
        "-X", $Method,
        "-H", "Authorization: Bearer $AccessToken",
        "-H", "Content-Type: application/json; charset=utf-8"
    )
    if ($Body -ne $null) {
        $tmp = [System.IO.Path]::GetTempFileName()
        try {
            $json = $Body | ConvertTo-Json -Depth 8
            [System.IO.File]::WriteAllText($tmp, $json, [System.Text.UTF8Encoding]::new($false))
            $args += @("--data-binary", "@$tmp")
            $args += $Url
            $output = & curl.exe @args
        } finally {
            Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
        }
    } else {
        $args += $Url
        $output = & curl.exe @args
    }
    if ($LASTEXITCODE -ne 0) {
        throw "curl failed with exit code $LASTEXITCODE for $Url"
    }
    return $output | ConvertFrom-Json
}

function Get-DocUrl {
    param([object]$Item)
    $existing = Get-FirstValue -Item $Item -Keys @("url")
    if ($existing) { return $existing }
    $token = Get-FirstValue -Item $Item -Keys @("docs_token", "obj_token")
    if (-not $token) { return "" }
    $type = Get-FirstValue -Item $Item -Keys @("docs_type", "obj_type")
    $type = $type.ToLowerInvariant()
    $map = @{
        "docx"    = "docx"
        "doc"     = "docs"
        "sheet"   = "sheets"
        "bitable" = "base"
        "wiki"    = "wiki"
        "slides"  = "slides"
    }
    $path = $map[$type]
    if (-not $path) { $path = "docx" }
    return ($FeishuWebBaseUrl.TrimEnd("/") + "/" + $path + "/" + $token)
}

function Search-Docs {
    param([string]$Query, [string]$AccessToken)
    $url = "$FeishuBaseUrl/open-apis/suite/docs-api/search/object"
    $body = @{
        search_key = $Query
        count      = [Math]::Min([Math]::Max($DocsPageSize, 1), 50)
        offset     = 0
        docs_types = @("doc", "docx")
    }
    $resp = Invoke-FeishuJson -Method "POST" -Url $url -AccessToken $AccessToken -Body $body
    return @($resp.data.docs_entities)
}

function Search-Wiki {
    param([string]$Query, [string]$AccessToken)
    $url = "$FeishuBaseUrl/open-apis/wiki/v2/nodes/search?page_size=$WikiPageSize"
    $body = @{ query = $Query }
    $resp = Invoke-FeishuJson -Method "POST" -Url $url -AccessToken $AccessToken -Body $body
    return @($resp.data.items)
}

function Get-RawContent {
    param([string]$DocsToken, [string]$AccessToken)
    $docxUrl = "$FeishuBaseUrl/open-apis/docx/v1/documents/$DocsToken/raw_content"
    $docUrl = "$FeishuBaseUrl/open-apis/doc/v2/$DocsToken/raw_content"
    foreach ($url in @($docxUrl, $docUrl)) {
        try {
            $output = & curl.exe --silent --show-error --location --noproxy "*" -X GET -H "Authorization: Bearer $AccessToken" $url
            if ($LASTEXITCODE -ne 0) { continue }
            $resp = $output | ConvertFrom-Json
            $content = [string]$resp.data.content
            if ($content) {
                return @{ content = $content; status = "" }
            }
        } catch {
            continue
        }
    }
    return @{ content = ""; status = "unavailable" }
}

function Add-Candidate {
    param(
        [hashtable]$Map,
        [object]$Item,
        [string]$SourceKind,
        [string]$Category,
        [string]$Query
    )
    $title = Get-FirstValue -Item $Item -Keys @("title", "name") -DefaultValue "Untitled"
    $docsType = Get-FirstValue -Item $Item -Keys @("docs_type", "obj_type")
    $docsToken = Get-FirstValue -Item $Item -Keys @("docs_token", "obj_token")
    $url = Get-DocUrl -Item $Item
    $key = if ($docsToken) { ($docsType + ":" + $docsToken).ToLowerInvariant() } elseif ($url) { ("url:" + $url).ToLowerInvariant() } else { ("title:" + $title).ToLowerInvariant() }

    if (-not $Map.ContainsKey($key)) {
        $Map[$key] = [ordered]@{
            title          = $title
            source_kind    = $SourceKind
            docs_type      = $docsType
            docs_token     = $docsToken
            url            = $url
            owner_id       = Get-FirstValue -Item $Item -Keys @("owner_id")
            raw_content    = ""
            raw_status     = ""
            hit_count      = 0
            categories     = New-Object System.Collections.ArrayList
            matched_queries= New-Object System.Collections.ArrayList
            source_queries = New-Object System.Collections.ArrayList
            score          = 0.0
            reasons        = New-Object System.Collections.ArrayList
        }
    }

    $candidate = $Map[$key]
    $candidate.hit_count += 1
    if (-not $candidate.categories.Contains($Category)) { [void]$candidate.categories.Add($Category) }
    if (-not $candidate.matched_queries.Contains($Query)) { [void]$candidate.matched_queries.Add($Query) }
    [void]$candidate.source_queries.Add(@{ category = $Category; query = $Query; source = $SourceKind })
}

function Get-TextMatchCount {
    param([string]$Text, [string[]]$Signals)
    $count = 0
    foreach ($signal in $Signals) {
        if ($Text -like "*$signal*") { $count += 1 }
    }
    return $count
}

function Score-Candidate {
    param([hashtable]$Doc)
    $score = 0.0
    $reasons = New-Object System.Collections.ArrayList

    $categoryCount = @($Doc.categories).Count
    if ($categoryCount -gt 0) {
        $score += $categoryCount * 3.5
        [void]$reasons.Add("category_coverage=$categoryCount")
    }

    if ($Doc.hit_count -gt 0) {
        $score += [Math]::Min([int]$Doc.hit_count, 8) * 1.5
        [void]$reasons.Add("hit_count=$($Doc.hit_count)")
    }

    $title = [string]$Doc.title
    $titlePositive = Get-TextMatchCount -Text $title -Signals $TitlePositiveSignals
    if ($titlePositive -gt 0) {
        $score += $titlePositive * 2.5
        [void]$reasons.Add("title_signal=$titlePositive")
    }

    $titleNegative = Get-TextMatchCount -Text $title -Signals $TitleNegativeSignals
    if ($titleNegative -gt 0) {
        $score -= $titleNegative * 3
        [void]$reasons.Add("title_noise=-$titleNegative")
    }

    $raw = [string]$Doc.raw_content
    if ($raw) {
        $score += 2
        [void]$reasons.Add("has_raw_content")
        $contentPositive = Get-TextMatchCount -Text $raw -Signals $ContentPositiveSignals
        if ($contentPositive -gt 0) {
            $score += [Math]::Min($contentPositive, 6) * 1.2
            [void]$reasons.Add("content_signal=$contentPositive")
        }
    } elseif ([string]$Doc.raw_status -eq "unavailable") {
        $score -= 0.5
        [void]$reasons.Add("raw_unavailable")
    }

    if ([string]$Doc.source_kind -eq "wiki") {
        $score += 1
        [void]$reasons.Add("wiki_source")
    }

    if ($title.Length -le 40) { $score += 0.5 }

    $Doc.score = [Math]::Round($score, 2)
    $Doc.reasons = $reasons
}

function Get-BatchSuffix {
    param([int]$CurrentBatchIndex, [int]$BatchCount, [int]$EffectiveBatchSize)
    if ($EffectiveBatchSize -le 0) {
        return ""
    }
    return ".batch-{0:d2}-of-{1:d2}" -f $CurrentBatchIndex, $BatchCount
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$accessToken = Get-UserToken -StorePath $TokenStore -TargetOpenId $OpenId
$queryMap = Get-Content -LiteralPath $QueriesFile -Raw -Encoding UTF8 | ConvertFrom-Json
$candidates = @{}
$queryPlan = New-Object System.Collections.ArrayList

foreach ($property in $queryMap.PSObject.Properties) {
    $category = [string]$property.Name
    $queries = @($property.Value)
    foreach ($query in $queries) {
        [void]$queryPlan.Add([PSCustomObject]@{
            Category = $category
            Query = [string]$query
        })
    }
}

$totalQueryCount = @($queryPlan).Count
$effectiveBatchSize = if ($BatchSize -gt 0) { $BatchSize } else { 0 }
$batchCount = if ($effectiveBatchSize -gt 0) { [int][Math]::Ceiling($totalQueryCount / [double]$effectiveBatchSize) } else { 1 }
if ($batchCount -lt 1) { $batchCount = 1 }
$currentBatchIndex = [Math]::Min([Math]::Max($BatchIndex, 1), $batchCount)
$batchSuffix = Get-BatchSuffix -CurrentBatchIndex $currentBatchIndex -BatchCount $batchCount -EffectiveBatchSize $effectiveBatchSize

if ($effectiveBatchSize -gt 0) {
    $start = ($currentBatchIndex - 1) * $effectiveBatchSize
    $selectedPlan = @($queryPlan | Select-Object -Skip $start -First $effectiveBatchSize)
} else {
    $selectedPlan = @($queryPlan)
}

Write-Host "[batch] index=$currentBatchIndex/$batchCount query_count=$($selectedPlan.Count)/$totalQueryCount sleep_ms=$QuerySleepMs"

for ($i = 0; $i -lt $selectedPlan.Count; $i++) {
    $entry = $selectedPlan[$i]
    $category = [string]$entry.Category
    $query = [string]$entry.Query
    Write-Host "[query] $($i + 1)/$($selectedPlan.Count) category=$category query=$query"
    try {
        $docs = @(Search-Docs -Query $query -AccessToken $accessToken)
        $wiki = @(Search-Wiki -Query $query -AccessToken $accessToken)
        foreach ($item in $docs) { Add-Candidate -Map $candidates -Item $item -SourceKind "docs" -Category $category -Query $query }
        foreach ($item in $wiki) { Add-Candidate -Map $candidates -Item $item -SourceKind "wiki" -Category $category -Query $query }
        Write-Host "  [hits] query=$query count=$($docs.Count + $wiki.Count)"
    } catch {
        Write-Warning "query=$query failed: $($_.Exception.Message)"
    }
    if ($QuerySleepMs -gt 0 -and $i -lt ($selectedPlan.Count - 1)) {
        Start-Sleep -Milliseconds $QuerySleepMs
    }
}

$candidateList = @($candidates.Values | Sort-Object @{Expression={ $_.categories.Count }; Descending=$true}, @{Expression={ $_.hit_count }; Descending=$true})
$rawTargets = @($candidateList | Select-Object -First $RawTopN)
foreach ($doc in $rawTargets) {
    $token = [string]$doc.docs_token
    $type = [string]$doc.docs_type
    if (-not $token -or @("doc","docx") -notcontains $type) { continue }
    $rawResult = Get-RawContent -DocsToken $token -AccessToken $accessToken
    $doc.raw_content = [string]$rawResult.content
    $doc.raw_status = [string]$rawResult.status
}

foreach ($doc in $candidateList) {
    Score-Candidate -Doc $doc
}

$ranked = @($candidateList | Sort-Object `
    @{Expression={ $_.score }; Descending=$true}, `
    @{Expression={ $_.categories.Count }; Descending=$true}, `
    @{Expression={ $_.hit_count }; Descending=$true} | Select-Object -First $TopN)

$jsonPath = Join-Path $OutputDir ("trusted_find_person_candidates{0}.json" -f $batchSuffix)
$csvPath = Join-Path $OutputDir ("trusted_find_person_candidates{0}.csv" -f $batchSuffix)

$jsonReady = foreach ($doc in $ranked) {
    [ordered]@{
        title = $doc.title
        source_kind = $doc.source_kind
        docs_type = $doc.docs_type
        docs_token = $doc.docs_token
        url = $doc.url
        owner_id = $doc.owner_id
        hit_count = $doc.hit_count
        categories = @($doc.categories)
        matched_queries = @($doc.matched_queries)
        score = $doc.score
        reasons = @($doc.reasons)
        raw_status = $doc.raw_status
        raw_preview = if ($doc.raw_content.Length -gt 500) { $doc.raw_content.Substring(0, 500) + "..." } else { $doc.raw_content }
        source_queries = @($doc.source_queries)
    }
}

$jsonReady | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $jsonPath -Encoding UTF8

$csvReady = foreach ($doc in $ranked) {
    [PSCustomObject]@{
        score = $doc.score
        title = $doc.title
        source_kind = $doc.source_kind
        docs_type = $doc.docs_type
        hit_count = $doc.hit_count
        category_count = @($doc.categories).Count
        categories = (@($doc.categories) -join " | ")
        matched_queries = (@($doc.matched_queries) -join " | ")
        url = $doc.url
        raw_status = $doc.raw_status
        reasons = (@($doc.reasons) -join " | ")
    }
}
$csvReady | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8

Write-Host ""
Write-Host "=== done ==="
Write-Host "candidate_count: $($candidateList.Count)"
Write-Host "batch_suffix: $(if ($batchSuffix) { $batchSuffix } else { '(full)' })"
Write-Host "output_json: $jsonPath"
Write-Host "output_csv : $csvPath"
Write-Host ""
Write-Host "Top 20 candidates:"
$idx = 1
foreach ($doc in ($ranked | Select-Object -First 20)) {
    $line = "{0:d2}. score={1,-5} cats={2} hits={3} title={4}" -f $idx, $doc.score, @($doc.categories).Count, $doc.hit_count, $doc.title
    Write-Host $line
    $idx += 1
}
