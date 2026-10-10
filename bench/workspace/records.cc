#include "records.h"

#include <algorithm>
#include <utility>

#include "absl/status/status.h"
#include "absl/strings/str_cat.h"
#include "absl/strings/str_format.h"
#include "absl/strings/str_join.h"
#include "absl/strings/str_split.h"

namespace records {

absl::StatusOr<Record> ParseLine(absl::string_view line) {
  std::vector<std::string> parts =
      absl::StrSplit(line, absl::MaxSplits(' ', 2));
  if (parts.size() != 3) {
    return absl::InvalidArgumentError(
        absl::StrCat("expected 3 fields, got ", parts.size()));
  }
  Record record;
  std::string error;
  if (!absl::ParseTime(absl::RFC3339_full, parts[0], &record.timestamp,
                       &error)) {
    return absl::InvalidArgumentError(absl::StrCat("bad timestamp: ", error));
  }
  record.level = std::move(parts[1]);
  record.message = std::move(parts[2]);
  return record;
}

absl::flat_hash_map<std::string, int> CountByLevel(
    const std::vector<Record>& records) {
  absl::flat_hash_map<std::string, int> counts;
  for (const Record& record : records) {
    ++counts[record.level];
  }
  return counts;
}

std::string Summary(const std::vector<Record>& records) {
  std::vector<std::pair<std::string, int>> counts;
  for (const auto& [level, count] : CountByLevel(records)) {
    counts.emplace_back(level, count);
  }
  std::sort(counts.begin(), counts.end());
  std::vector<std::string> parts;
  for (const auto& [level, count] : counts) {
    parts.push_back(absl::StrFormat("%s=%d", level, count));
  }
  return absl::StrJoin(parts, ", ");
}

}  // namespace records
