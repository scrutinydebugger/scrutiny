#include <gtest/gtest.h>
#include <cstdlib>
#include <cstdint>

#include "scrutiny.h"

#define ASSERT_BUF_EQ(a,b,c) ASSERT_TRUE(COMPARE_BUF(a, b, c))
#define EXPECT_BUF_EQ(a,b,c) EXPECT_TRUE(COMPARE_BUF(a, b, c))

class ScrutinyTest : public ::testing::Test
{
protected:

	void add_crc(uint8_t* data, uint16_t data_len);
	void add_crc(scrutiny::Protocol::Response* response);
	void fill_buffer_incremental(uint8_t* buffer, uint32_t length);
	unsigned int encode_addr(uint8_t* buffer, void* addr);

	::testing::AssertionResult COMPARE_BUF(const uint8_t* candidate, const uint8_t* expected, const uint32_t size);
	::testing::AssertionResult IS_PROTOCOL_RESPONSE(uint8_t* buffer, scrutiny::Protocol::CommandId cmd, uint8_t subfunction, scrutiny::Protocol::ResponseCode code);

};