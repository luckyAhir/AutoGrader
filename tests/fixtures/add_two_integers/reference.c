/* Reference solution for add_two_integers (instructor's oracle).
 * The sum of two 32-bit ints always fits in a 64-bit long long. */
#include <stdio.h>

int main(void)
{
    long long lhs, rhs;
    if (scanf("%lld %lld", &lhs, &rhs) != 2) {
        return 1;
    }
    printf("%lld\n", lhs + rhs);
    return 0;
}
