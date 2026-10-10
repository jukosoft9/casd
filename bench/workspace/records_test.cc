#include "records.h"

#include "gtest/gtest.h"

namespace records {
namespace {

TEST(ParseLine, AcceptsAWellFormedLine) {
  auto record = ParseLine("2026-10-10T12:00:00+00:00 ERROR disk is full");
  ASSERT_TRUE(record.ok());
  EXPECT_EQ(record->level, "ERROR");
  EXPECT_EQ(record->message, "disk is full");
  EXPECT_EQ(absl::ToUnixSeconds(record->timestamp), 1791633600);
}

TEST(ParseLine, RejectsMissingFieldsAndBadTimestamps) {
  EXPECT_FALSE(ParseLine("ERROR").ok());
  EXPECT_FALSE(ParseLine("yesterday ERROR disk is full").ok());
}

TEST(Summary, CountsAndSortsByLevel) {
  std::vector<Record> parsed;
  for (const char* line : {"2026-10-10T12:00:00+00:00 INFO a",
                           "2026-10-10T12:00:01+00:00 ERROR b",
                           "2026-10-10T12:00:02+00:00 INFO c"}) {
    parsed.push_back(*ParseLine(line));
  }
  EXPECT_EQ(Summary(parsed), "ERROR=1, INFO=2");
}

}  // namespace
}  // namespace records
