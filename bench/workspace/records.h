#ifndef CASD_BENCH_RECORDS_H_
#define CASD_BENCH_RECORDS_H_

#include <string>
#include <vector>

#include "absl/container/flat_hash_map.h"
#include "absl/status/statusor.h"
#include "absl/strings/string_view.h"
#include "absl/time/time.h"

namespace records {

struct Record {
  absl::Time timestamp;
  std::string level;
  std::string message;
};

// Parses "<RFC 3339 timestamp> <LEVEL> <message>".
absl::StatusOr<Record> ParseLine(absl::string_view line);

absl::flat_hash_map<std::string, int> CountByLevel(
    const std::vector<Record>& records);

// "ERROR=2, INFO=5", sorted by level.
std::string Summary(const std::vector<Record>& records);

}  // namespace records

#endif  // CASD_BENCH_RECORDS_H_
